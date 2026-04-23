def stateTransition:
    for retry in range(0, retryLimit):
        resp = mp.HTTPGET(API*)
        if resp.Code == 200:
            privaterepo.EnablePrivateRepo()
            upgradehelper.upgrade()
        elif resp.Code == 403
            match resp.errMsg:
                case "disabled"
                    error()
                case "refreshing"
                    continue # Retry till next round
                case "provisioning"
                    continue
                case _
                    error("unsupported state")
        else
            error("token not found")
