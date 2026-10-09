# hermes-web-fallback-chain

Hermes Agent 插件：为 `web_search` / `web_extract` 提供加权 fallback 链。

- chain 前两个 provider 为 primary，按 **2:1** 加权轮询分摊并发流量（默认 firecrawl 2/3、tavily 1/3）
- 当次请求的 primary 失败（异常/空结果/缺 key）→ 自动切另一个 primary → 再落 chain 尾部（searxng）
- 两个 primary 都不可用时，全部流量直接走 searxng
- 线程安全：`itertools.cycle.__next__` 原子分发，并发突发自动摊开
- chain 里未注册的名字自动跳过

## 安装

```bash
# 1. 克隆到 Hermes 用户插件目录（重启网关后生效）
git clone https://github.com/<you>/hermes-web-fallback-chain.git ~/.hermes/plugins/web-fallback-chain

# 2. 配置（chain 前两个是 primary，权重固定 2:1；后面的按序兜底）
hermes config set web.backend fallback-chain
hermes config set web.chain '["firecrawl","tavily","searxng"]' --force

# 3. 确保各 provider 的 key 就绪
#    FIRECRAWL_API_KEY / TAVILY_API_KEY / SEARXNG_URL

# 4. 重启网关
systemctl --user restart hermes-gateway.service
```

## 在 hermes tools 里选用

`hermes tools` → **🔍 Web Search & Scraping** → 选 **Fallback chain**（badge: custom）。
等价于 `web.backend: fallback-chain` 配置。

## 验证

```bash
grep -aE "fallback-chain: (primary|search succeeded)" ~/.hermes/logs/agent.log | tail
# 期望看到 primary 在 firecrawl/tavily 间轮换（2:1），失败时自动切换
```

或离线验证：

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from hermes_cli.plugins import discover_plugins; discover_plugins()
import agent.web_search_registry as reg
p = reg._registry.merged()['fallback-chain']
print(p.search('test', limit=2).get('success'))"
```
