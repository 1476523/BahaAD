"""GitHub 登入（完全可選）／star 提醒／同帳號多 IP 心跳防護。規格見
docs/requirements/access_gate.md。

只跟 `access_gate_server`（獨立部署、不隨 `main.exe` 散布的小型後端，見
`docs/requirements/access_gate_server.md`）對話——`main.exe` 全程只持有一組不透明、
可撤銷的 `bahaad_token`，從來拿不到真正的 GitHub access token，見 access_gate.md
「重要架構決策」一節。
"""
