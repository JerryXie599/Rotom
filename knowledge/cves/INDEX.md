# CVE 速查索引(组件/指纹 → 卡)

**用途**:跑分/众测里一旦指纹出组件名(或指纹出"这是某类 AI 服务"),先来这里按组件命中卡,
拿到"该组件常见的公开漏洞 + 最短验证路径",再去目标上**确认版本**、只打目标未修的那几个。

**用法**
```bash
python3 tools/kb.py hit "comfyui"        # 指纹命中(卡片目录 + 本目录都会搜)
python3 tools/kb.py hit "grafana 8.2"
python3 tools/kb.py show cves/web-midware # 看全文
```

**三条纪律(比 CVE 号本身更重要)**
1. **先读版本,再套用**:同一组件不同版本的漏洞面完全不同;版本拿不到就先去拿(见 [find-cve.md](find-cve.md))。
2. **只打目标版本未修的**:卡里给的 CVE 是**检索锚点**,命中后要用官方公告/上游补丁核对版本区间。
3. **用上游修复 PR 的测试文件当验证清单**:补丁自带的安全测试就是"这个洞该怎么验"的最短说明书(实测最快的一条路)。

> 本目录只写**公开技术信息**(厂商公告/GHSA/NVD/上游补丁),不含任何平台题目答案、题号或靶场数据;
> 带具体 CVE 号的条目都是可以公开检索到的通告,拿不准的地方只写方法、不编号,防止把你带偏。

| 卡 | 什么时候看 |
|---|---|
| [find-cve.md](find-cve.md) | **通用**:拿到指纹/版本后,怎么最快找到"这个版本确实存在"的漏洞与验证方法 |
| [web-midware.md](web-midware.md) | 指纹出经典 Web 中间件/框架/运维组件(网关、CI、搜索、监控、缓存、图数据库、企业应用……) |
| [ai-service.md](ai-service.md) | 指纹出 **AI/ML 服务**(推理、编排、工作流、模型管理、Notebook)—— 众测里越来越多 |
| [token-and-sandbox.md](token-and-sandbox.md) | 登录态是 JWT/令牌、或目标提供"在线跑代码"的沙箱(vm2/Python/模板)—— 绕过校验类 |

**每张卡末尾都有一张 CVE 表**,并把条目分成三档:`✅实测`(在真实目标上看到路由/版本并验证过)、
`📋公告`(公开通告的经典条目)、`⚠候选`(检索上下文里出现但**未核实** —— 用前必须按上游公告核对)。
最近一轮跑分涉及的组件(ComfyUI/Manager、Langflow、Gradio、Dify、PyTorch、HugeGraph、GeoServer、OFBiz、ActiveMQ、
1Panel、OpenSSH、OpenSSL、vm2、JWT 系列)都已收录。