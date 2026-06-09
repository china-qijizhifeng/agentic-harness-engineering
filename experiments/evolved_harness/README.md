# code_agent_simple

仅注册 `run_shell_command` 的精简 NexAU code agent，配置见 `code_agent.yaml`。

## 本地测试

```bash
export LLM_MODEL=... LLM_BASE_URL=... LLM_API_KEY=...
export SANDBOX_WORK_DIR=/tmp/code_agent_work   # 建议
python3 start.py
```

或通过 NexAU CLI（需本目录为工作目录且已安装 `nexau`）：

```bash
nexau run code_agent
```
