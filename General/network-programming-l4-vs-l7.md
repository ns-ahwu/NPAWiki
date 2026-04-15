# Network Programming in NPA: L4 vs L7

## Overview

There are two distinct levels of network programming in this codebase, both running on the **same single-threaded epoll event loop**, but sitting at very different abstraction heights.

| Layer | Representative Files | Go Equivalent |
|---|---|---|
| **L4 — Raw socket / TUN** | `nshTunHandler.h/.cpp`, `dispatcherEpoll.h/.cpp`, `handler.h` | `net.Listen()` + manual `conn.Read()` |
| **L7 — HTTP client** | `httpclient.h/.cpp`, `upgradechecker.cpp` | `http.Get()` with response callback |

---

## The Common Foundation: The epoll Event Loop

Both layers are driven by the **same** dispatcher loop in `DispatcherEpoll::startDispatchSync()`:

```cpp
// npa_core/src/shared/dispatcherEpoll.cpp

void DispatcherEpoll::startDispatchSync() {
    struct epoll_event *events = calloc(MAX_EVENTS, sizeof(epoll_event));

    do {
        int numEvents = epoll_wait(m_efd, events, MAX_EVENTS, timeout); // block until I/O
        dispatchFuncs();               // run deferred lambdas
        dispatchTimers();              // fire expired timers
        dispatchEvents(numEvents, events); // route I/O events to Handlers
    }
    while (!m_stopDispatcher);
}
```

`dispatchEvents()` calls `doDispatch(fd, event)` which routes to the correct virtual method on the registered `Handler` object:

```
EPOLLIN   → handler->handleRx(fd)       // data is available to read
EPOLLOUT  → handler->handleWReady(fd)   // socket is write-ready (also used for connect)
EPOLLRDHUP→ handler->handleClose(fd)    // peer closed the connection
```

This is the **heartbeat** of the entire system.

---

## Layer 1: L4 — Raw fd / TUN Device

### The Handler contract

`Handler` (`npa_core/src/shared/handler.h`) is the abstract base class. Every L4 component subclasses it and implements three virtual methods:

```cpp
class Handler {
public:
    // Called when fd has data to read. Return false → dispatcher shuts down fd.
    virtual bool handleRx(int fd) = 0;

    // Called when fd becomes write-ready (used for async connect detection).
    virtual bool handleWReady(int fd) { assert(0); return false; }

    // Called when fd should be closed and cleaned up.
    virtual void handleClose(int fd) = 0;
};
```

### NshTunHandler: wiring a TUN device into the loop

`NshTunHandler` subclasses `Handler` directly. It wraps the Linux TUN virtual network interface (`/dev/tun0`), which presents as a raw file descriptor that produces and consumes IPv4 packets.

**Registration** (`npa_gateway/src/proxy/nshTunHandler.cpp`):

```cpp
void NshTunHandler::init() {
    SocketTools::makeSocketNonBlocking(m_tunFd);

    // Tell the dispatcher: when m_tunFd fires, call this->handleRx / handleClose / handleWReady
    Dispatcher::getDispatcher().registerFd(m_tunFd, this, this, this);
    //                                              ^rx   ^close ^write

    Dispatcher::getDispatcher().pauseTx(m_tunFd); // don't watch EPOLLOUT yet
}
```

Internally, `registerFd` calls `epoll_ctl(EPOLL_CTL_ADD)`:

```cpp
// npa_core/src/shared/dispatcherEpoll.cpp
void DispatcherEpoll::registerFd(int fd, Handler *rx, Handler *close, Handler *write) {
    DispatcherBase::registerFd(fd, rx, close, write); // store in m_handlersMap
    struct epoll_event event;
    event.data.fd = fd;
    event.events  = EPOLLIN | EPOLLRDHUP;
    if (write != NULL) event.events |= EPOLLOUT;
    epoll_ctl(m_efd, EPOLL_CTL_ADD, fd, &event);
}
```

**What `handleRx` sees** — raw IPv4 packets:

```cpp
bool NshTunHandler::handleRx(int fd) {
    ssize_t nread = read(m_tunFd, m_tunBuffer, m_tunBufSize); // raw syscall

    // m_tunBuffer is a raw IPv4 packet — no protocol parsing done for you
    struct ip    *ipV4Header = (struct ip*)m_tunBuffer;
    struct tcphdr *tcpHeader = (struct tcphdr*)(m_tunBuffer + IP_HDR_LEN(ipV4Header->ip_hl));
    uint16_t sourcePort      = ntohs(tcpHeader->source);

    // Route the packet based on custom ZTNA logic
    uint32_t connId = SourcePortMapper::getInstance().getMapping(sourcePort);
    // ...forward to the right ProxyConnectionHandler
}
```

**Key characteristics:**

- Callback fires on **every read-ready event** — even a single TCP segment
- You receive raw bytes and manually cast to `struct ip*`, `struct tcphdr*`
- No state machine — you own all connection state
- `return false` from `handleRx` signals the dispatcher to initiate shutdown of that fd

---

## Layer 2: L7 — HTTP Client

### The layering

`HttpClient` does **not** subclass `Handler` directly. It owns a `TlsTcpClientServer` member which IS a Handler internally. `HttpClient` sits one level above:

```
Handler (abstract)
    └── TlsTcpClientServer      ← registered with Dispatcher, handles raw TLS/TCP bytes
            │
            └── rxCallback ──→  HttpClient::rxHandler()
                                    │
                                    └── http_parser (state machine)
                                            │
                                            ├── on_status()           ← HTTP status code
                                            ├── on_body()             ← accumulates body chunks
                                            └── on_message_complete() ← triggers final delivery
                                                    │
                                                    └── HttpClient::handleResponse()  ← YOUR callback
                                                            │
                                                            └── UpgradeChecker::handleResponse()
```

### HttpClient state machine

Before `handleResponse` is ever called, the HTTP client walks through a built-in state machine:

```
e_http_init
    → e_http_connecting     (TCP SYN sent)
    → e_http_tls_handshake  (TLS ClientHello / ServerHello)
    → e_http_processing     (request sent, reading response)
    → e_http_finish         (full HTTP response received and parsed)
```

```cpp
// npa_core/src/shared/httpclient.cpp
void HttpClient::start(const char *request, size_t reqlen) {
    setState(e_http_connecting);

    m_tcpClient->setConnectCallback([this](uint32_t err) {
        if (err == 0) setState(e_http_tls_handshake); // or e_http_processing if no TLS
    });
    m_tcpClient->setTunnelEstabCb([this](const char *tlsVer) {
        setState(e_http_processing);
    });

    m_tcpClient->start([this](char *buf, int len, void *meta, bool err) {
        this->rxHandler(buf, len, err); // wire up the raw byte callback
    });

    m_timeoutTimer.start(m_timeout); // arm 150s watchdog
    m_tcpClient->send(request, reqlen);
}
```

### Two callback styles

**Style 1 — Subclass + virtual override** (used by `UpgradeChecker`):

```cpp
// npa_gateway/src/stitcher/upgradechecker.cpp

// UpgradeChecker extends HttpClient and overrides handleResponse
void UpgradeChecker::handleResponse(const char *buf, size_t len, bool err) {
    handleResponseInternal(buf, len, err);
    deleteLater(); // schedule "delete this" on next dispatcher cycle
}

void UpgradeChecker::handleResponseInternal(const char *buf, size_t len, bool err) {
    if (err) {
        DebugLog(log_error, "Checking with ORCA for publisher upgrade failed. code: %d", getStatus());
        return;
    }

    // buf is already: HTTP headers stripped, body assembled, null-terminated
    std::string parseerr;
    auto recvdJson = json11::Json::parse(std::string(buf, len), parseerr);
    auto publishers = recvdJson["publishers"].array_items();
    m_publisherCallback(publishers, false);
}
```

**Style 2 — Lambda / `std::function`** (for one-off use without subclassing):

```cpp
// npa_core/src/shared/httpclient.h
void setCallBack(std::function<void (char *buf, size_t len, bool err)> responseCallback);

// Usage at call site:
httpClient->setCallBack([](char *buf, size_t len, bool err) {
    // handle response inline — no subclass needed
});
```

### What handleResponse sees

Compared to L4, the data arriving here is already fully processed:

```cpp
// By the time you get here:
//   - TCP fragmentation has been reassembled
//   - TLS has been decrypted
//   - HTTP headers have been parsed and stripped
//   - Status code has been validated (non-2xx → err=true)
//   - Chunked transfer encoding has been decoded
//   - Body has been accumulated into a single buffer
//   - Timeout watchdog has been managed

void UpgradeChecker::handleResponseInternal(const char *buf, size_t len, bool err) {
    auto recvdJson = json11::Json::parse(std::string(buf, len), parseerr);
    // ^ just parse JSON — that's all you need to do
}
```

---

## Side-by-Side Comparison

| Dimension | L4: NshTunHandler | L7: HttpClient |
|---|---|---|
| **Abstraction** | Raw IPv4 packets | Assembled, parsed HTTP response body |
| **Who is a `Handler`?** | `NshTunHandler` itself | Inner `TlsTcpClientServer` — HttpClient wraps it |
| **Callback style** | Virtual override of `handleRx(int fd)` | Virtual override of `handleResponse()` **or** `std::function` lambda |
| **Callback fires** | On every readable event (each raw packet arrival) | Once per full HTTP exchange |
| **What you parse** | `struct ip*`, `struct tcphdr*` — raw byte offsets | JSON string — all protocol layers already handled |
| **State machine** | None — you own all state | Built-in: init → connecting → TLS → processing → finish |
| **Error signal** | `return false` from `handleRx` | `err=true` param + `te_http_client_error` enum |
| **Timeout** | Not built-in (manage yourself) | Built-in `Timer` (default 150s), `m_isTimedOut` flag |
| **Lifecycle** | Manual `registerFd` / `unregisterFd` | `deleteLater()` defers `delete this` to next loop cycle |
| **Go equivalent** | `net.Listen()` + `conn.Read()` | `http.Get()` / `http.HandleFunc()` |

---

## The Canonical Mental Model

```
epoll_wait fires EPOLLIN on some fd
        │
        ├─── L4 path ──────────────────────────────────────────────────────────────────────
        │    doDispatch() → NshTunHandler::handleRx(fd)
        │                        │
        │                        └─ read(tunFd, buf, 9000)    // raw syscall
        │                           struct ip    *iph  = (struct ip*)buf
        │                           struct tcphdr *tcp = (struct tcphdr*)(buf + ip_hl*4)
        │                           // YOU parse, YOU route, YOU own the state
        │
        └─── L7 path ──────────────────────────────────────────────────────────────────────
             doDispatch() → TlsTcpClientServer::handleRx(fd)
                                 │
                                 └─ TLS decrypt
                                    HttpClient::rxHandler(buf, len, err)
                                         │
                                         └─ http_parser_execute()
                                            on_body() accumulates chunks
                                            on_message_complete()
                                                 │
                                                 └─ UpgradeChecker::handleResponse(body, len, err)
                                                    // YOU get clean JSON body — nothing else to do
```

---

## Why Each Layer Exists in NPA

**L4 is used where the Publisher must _be_ the network.**

The TUN device intercepts raw IP traffic. The Publisher needs to inspect IP headers and TCP source ports to map packets to the correct internal ZTNA connection (via `SourcePortMapper`), then forward them to the right `ProxyConnectionHandler`. There is no standard protocol here — the tunnel format is custom. Delegating to a library is not possible.

**L7 is used where the Publisher is a _client_ of a standard service.**

When talking to the Orca management plane (e.g., `/orca/publishers/upgrade_check`), the protocol is plain HTTPS + JSON. There is no reason to hand-parse HTTP when a correct, battle-tested library exists. `HttpClient` wraps that complexity and surfaces only the response body.

---

## Appendix: Is This Canonical? References and Search Keywords

### Short answer: Yes — this is textbook

The `Dispatcher` / `Handler` pattern in this codebase is a direct C++ implementation of the **Reactor pattern**, one of the most well-documented architectural patterns in systems programming. It was formally named and described by **Douglas C. Schmidt** in 1994 and has since appeared in every major text on concurrent and networked systems.

---

### The Core Pattern: Reactor

**Formal name:** _Reactor — An Object Behavioral Pattern for Demultiplexing and Dispatching Handles for Synchronous Events_
**Author:** Douglas C. Schmidt, 1994

The mapping to this codebase is exact:

| Reactor vocabulary | This codebase |
|---|---|
| **Reactor** | `DispatcherEpoll` |
| **Handle** | file descriptor (`int fd`) |
| **Event Demultiplexer** | `epoll_wait()` |
| **Concrete Event Handler** | `NshTunHandler`, `TlsTcpClientServer`, etc. |
| **Event Handler (abstract)** | `Handler` base class (`handleRx`, `handleClose`, `handleWReady`) |
| **Initiation Dispatcher** | `registerFd()` / `unregisterFd()` / `doDispatch()` |

The pattern answers the question: _"How do you handle many concurrent I/O sources in a single thread without blocking?"_

```
                    ┌─────────────────────────────┐
                    │         Reactor              │
                    │      (DispatcherEpoll)        │
                    │                              │
  register(fd, h) ──►  m_handlersMap[fd] = h      │
                    │                              │
  startDispatch() ──►  loop:                      │
                    │    epoll_wait()              │
                    │    for each event:           │
                    │      h->handleRx(fd)  ◄──────┼── Concrete Handler
                    │      h->handleClose() ◄──────┼── (NshTunHandler, etc.)
                    └─────────────────────────────┘
```

---

### The Companion Pattern: Proactor

`HttpClient` is closer to the **Proactor pattern** (also by Schmidt). The distinction:

| Pattern | Trigger | What you get in callback |
|---|---|---|
| **Reactor** | "The fd is *ready* — go read it yourself" | raw fd, you call `read()` |
| **Proactor** | "The *operation is complete* — here is the result" | assembled result (HTTP body) |

`HttpClient` behaves as a Proactor: by the time `handleResponse()` fires, the async operation (TCP connect + TLS + HTTP parse) is fully complete and the result is handed to you.

---

### The Motivation: The C10K Problem

**"The C10K Problem"** by Dan Kegel (1999) is the canonical document explaining _why_ `select()`/`poll()` don't scale and why `epoll` (Linux), `kqueue` (BSD/macOS), and `IOCP` (Windows) were invented. It directly motivates the architecture in this codebase.

The core insight: a server handling 10,000 simultaneous connections cannot afford one thread per connection. The solution is one event loop thread + non-blocking I/O + callbacks.

```
Old model (one thread per connection):
    conn_1 → thread_1 → blocking read() → wakes up → processes
    conn_2 → thread_2 → blocking read() → wakes up → processes
    ...
    conn_10000 → thread_10000 → ... (10k threads = OOM)

New model (Reactor / epoll):
    all conns → one thread → epoll_wait() → dispatch to handler
    (1 thread handles 10k connections)
```

This codebase follows the new model. `DispatcherEpoll` is the single-threaded event demultiplexer.

---

### Real-World Implementations of the Same Pattern

These widely-used libraries all implement the Reactor pattern. Studying any of them deepens understanding of this codebase:

| Library / System | Language | Reactor equivalent | Notes |
|---|---|---|---|
| **ACE** (Adaptive Communication Environment) | C++ | `ACE_Reactor` | Schmidt's own reference implementation; most similar to this codebase |
| **libevent** | C | `event_base` | Used by memcached, Tor |
| **libev** | C | `ev_loop` | Lighter than libevent |
| **libuv** | C | `uv_loop_t` | Powers Node.js; also implements Proactor-style async ops |
| **Boost.Asio** | C++ | `io_context` | Most widely used C++ async I/O library; supports both Reactor and Proactor styles |
| **Netty** | Java | `EventLoop` / `ChannelHandler` | Direct Java analogue: `ChannelHandler` = `Handler`, `EventLoopGroup` = `DispatcherEpoll` |
| **Tokio** | Rust | `Runtime` / `AsyncRead` | Modern async runtime; same concept, Rust ownership model |
| **Node.js** | JavaScript | event loop (via libuv) | The most famous single-threaded event loop |

---

### Book References

| Book | Relevance |
|---|---|
| **"Pattern-Oriented Software Architecture Vol. 2: Patterns for Concurrent and Networked Objects"** — Schmidt, Stal, Rohnert, Buschmann (2000) | The authoritative book. Chapters on Reactor, Proactor, Acceptor-Connector, and Half-Sync/Half-Async directly describe this codebase's architecture. |
| **"Unix Network Programming Vol. 1"** — W. Richard Stevens (3rd ed. 2003) | The bible of POSIX socket programming. Chapter 6 covers `select`/`poll`/`epoll` I/O multiplexing. Essential background for the L4 layer. |
| **"The Linux Programming Interface"** — Michael Kerrisk (2010) | Chapter 63 covers `epoll` in depth — exactly what `DispatcherEpoll` uses. |

---

### Search Keywords

Use these to find more material online:

```
# The core pattern
"Reactor pattern"
"Reactor pattern C++"
"Douglas Schmidt Reactor pattern"
"POSA2 Reactor"

# The Linux mechanism underneath
"epoll event loop"
"Linux epoll tutorial"
"non-blocking socket programming Linux"
"epoll_wait EPOLLIN EPOLLOUT example"

# The motivation
"C10K problem"
"event-driven network programming"
"single-threaded event loop"

# Concrete libraries to study
"Boost.Asio tutorial"          ← best modern C++ analogue
"libevent tutorial"            ← closest C analogue
"ACE Reactor framework"        ← closest historical C++ analogue
"Netty ChannelHandler example" ← closest Java analogue

# The two-layer abstraction specifically
"Reactor vs Proactor pattern"
"async I/O completion callback"
"HTTP client over async TCP C++"
```

---

### How the NPA Code Maps to the Wider Ecosystem

```
This codebase          Boost.Asio equivalent       Node.js equivalent
──────────────         ──────────────────────       ──────────────────
DispatcherEpoll    ≈   io_context                ≈  libuv event loop
Handler            ≈   async_read_some handler   ≈  EventEmitter
registerFd()       ≈   async_read_some()         ≈  socket.on('data', cb)
NshTunHandler      ≈   custom Protocol object    ≈  net.Socket with 'data' handler
HttpClient         ≈   Beast HTTP async client   ≈  http.get() callback
```

The NPA implementation is hand-rolled (no external event library dependency), which is common in embedded/appliance software for control over binary size, latency, and platform-specific tuning. The pattern it follows, however, is entirely standard.
