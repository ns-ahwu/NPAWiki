# Project Name
npa_publisher_wizard, The npa_publisher_wizard (often referred to as the Publisher installation or registration wizard/script) is a guided process within the Netskope UI that assists administrators in deploying and registering a Publisher. It simplifies generating the required token, downloading the VHD/OVA image, and configuring secure connectivity between private apps and the Netskope cloud. A Golang application that manages the backend logic and API interactions.

## Tech Stack
- Language: Golang

## Rules
- Do not write unit test right after complete the business logic
- Instead, run `makeAndScp --wiz` to build the Golang binary
- Also if I said this need an E2E test on the test stack, run `makeAndScp --wiz --scp <STACK_NAME>` this will locally build the binary and scp it to the test stack for E2E testing. I will explicitly say we need an E2E test for a specific feature, so please do not proceed to add unit tests until I said the E2E test is successful. The reason behind this is that we want to make sure the feature works in the real environment before we spend time writing unit tests, and also some of the business logic code may not be fully covered by unit tests, so we want to make sure we have a successful E2E test before we write unit tests to cover all the business logic code.
- Only write unit tests after the E2E test is successful, and make sure to cover all the business logic code with unit tests
- To run the all unit tests, please run `make test` under container npa_publisher_wizard_dev, you can use `docker exec -it npa_publisher_wizard_dev bash` to get into the container first, then run the test command. You can also run specific test files with `go test -v <TEST_FILE_NAME>`. But remember to run all the tests in the container not the host machine, since some of the envs are only set in the container.
```
docker ps
CONTAINER ID   IMAGE                      COMMAND                  CREATED         STATUS         PORTS     NAMES
15930ec3c4c8   npa_publisher_wizard_dev   "bash"                   2 minutes ago   Up 2 minutes             npa_publisher_wizard_dev
f3a145d520a1   047ee13c9b12               "/bin/bash -l automa…"   2 years ago     Up 27 hours              great_chaum
```

## Commands
- Run all test: `docker exec $(docker ps --filter "ancestor=npa_publisher_wizard_dev" --format "{{.ID}}") sh -c "git config --global --add safe.directory /go/src/github.com/netSkope/npa_publisher_wizard && make test"`
- Build: `makeAndScp --wiz`
- Build and scp to test stack: `makeAndScp --wiz --scp <STACK_NAME>`
