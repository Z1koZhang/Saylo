# Security and privacy

## 永远不要提交

- `assistant/secrets.json`、`assistant/secrets.enc`、`.env`、任何 `*.key`
- `assistant/store/` 中除 `.gitkeep` 以外的所有内容（含 `store/secure/` 全部密文）
- `assistant/store/records.key.json` 与 `assistant/store/records.key.dpapi`（记录主密钥包装）
- `*.jsonl` 对话流水、记忆文件、提醒数据
- `*.senc`、`*.slog` 加密记录与加密日志
- `*.npy`、`*.npz`、向量索引和原始语料目录
- `*.log`、`*.pid`、崩溃转储和聊天截图
- 真实微信 ID、联系人名称、白名单和本机用户路径

加密密钥文件也不应提交。加密只能降低误读风险，不能把私人数据变成适合公开的数据。
`store/secure/` 下的内容即使已加密，也属于私密运行数据，同样不要提交；DPAPI 包装的
主密钥绑定当前 Windows 用户，泄露后配合该用户环境仍可解密。

## 每次推送前

```powershell
python tools\privacy_check.py
git status --short
git diff --cached
```

如果某个敏感文件已经进入 Git 历史，仅在最新提交中删除是不够的。应立即撤销或轮换密钥，
并使用 `git filter-repo` 清理整个历史后再推送。

## 微信进程内存修改

无障碍恢复机制可能写入运行中的微信进程内存。它只允许已验证版本、只写一个状态字节，失败
时恢复原值，但仍可能造成客户端不稳定或触发平台风控。使用者需要自行承担并评估这项风险。

