#!/usr/bin/env python3
"""读取 Codex 5h/周额度,输出 JSON 到 stdout。

和 cc-switch 同源:读 ~/.codex/auth.json 的 OAuth token,调
chatgpt.com/backend-api/wham/usage,token 过期自动用 refresh_token 刷新。

⚠️ wham/usage 是 OpenAI 的**非公开后端接口**(ChatGPT 客户端自己用),没有文档、不保证稳定,
   OpenAI 一旦改 URL/字段/鉴权,本脚本就会失效——届时 Codex 额度拿不到(不影响 Claude 额度)。

代理(可选,零硬编码):设环境变量 CODEX_QUOTA_PROXY=http://127.0.0.1:7897 走代理
(国内访问 chatgpt.com 多半需要);不设则直连。
"""
import json, sys, os, urllib.request, urllib.parse, urllib.error, datetime

AUTH_FILE = os.path.expanduser("~/.codex/auth.json")
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"   # OpenAI 公开 client_id(与 cc-switch 同源)
PROXY = os.environ.get("CODEX_QUOTA_PROXY", "").strip()


def _opener():
    if PROXY:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))
    return urllib.request.build_opener()


def load_auth():
    return json.load(open(AUTH_FILE))


def save_auth(d):
    with open(AUTH_FILE, "w") as f:
        json.dump(d, f, indent=2)


def refresh_token(d):
    rt = d["tokens"]["refresh_token"]
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data,
          headers={"Content-Type": "application/x-www-form-urlencoded"})
    r = _opener().open(req, timeout=15)
    result = json.loads(r.read().decode())
    d["tokens"]["access_token"] = result["access_token"]
    if "refresh_token" in result:
        d["tokens"]["refresh_token"] = result["refresh_token"]
    if "id_token" in result:
        d["tokens"]["id_token"] = result["id_token"]
    d["last_refresh"] = datetime.datetime.utcnow().isoformat() + "Z"
    save_auth(d)
    return d


def fetch_usage(d):
    token = d["tokens"]["access_token"]
    account_id = d["tokens"].get("account_id", "")
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + token,
        "ChatGPT-Account-Id": account_id,
    })
    return json.loads(_opener().open(req, timeout=15).read().decode())


def main():
    try:
        d = load_auth()
    except Exception as e:
        print(json.dumps({"error": "auth.json: " + str(e)}))
        sys.exit(1)

    # 先试一次,403 就刷新 token 再试
    for attempt in range(2):
        try:
            data = fetch_usage(d)
            rl = data.get("rate_limit", {})
            p = rl.get("primary_window", {})
            s = rl.get("secondary_window") or {}
            print(json.dumps({
                "primary": {"usedPercent": p.get("used_percent", 0),
                            "resetsAt": p.get("reset_at", 0)},
                "secondary": {"usedPercent": s.get("used_percent", 0),
                              "resetsAt": s.get("reset_at", 0)},
                "planType": data.get("plan_type", ""),
            }))
            return
        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt == 0:
                try:
                    d = refresh_token(d)
                except Exception as re:
                    print(json.dumps({"error": "token refresh: " + str(re)}))
                    sys.exit(1)
            else:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
        except Exception as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)


if __name__ == "__main__":
    main()
