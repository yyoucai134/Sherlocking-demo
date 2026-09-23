"""Multi-Agent 凭证发现 — CodexCredentialProvider + ClaudeCredentialProvider。

复用 Codex / Claude Code CLI 登录态，Sherlocking 不管理凭证（只发现，不刷新/不存储）。
凭证不进 LLM 主态（INV#8）：只传给 specialist runtime，不写 ThreadState。
"""
