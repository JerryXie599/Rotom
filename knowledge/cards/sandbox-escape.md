# 卡:沙箱逃逸(代码执行被限制)

**适用**:能提交代码/表达式,但被限制在解释器或模板沙箱里(常见于"执行代码拿 flag"类题目)。

## 按沙箱类型分
| 沙箱 | 起点 | 思路 |
|---|---|---|
| Python `pickle` / `yaml.load` / `eval` 受限 | 能传序列化数据或表达式 | 找 `__reduce__`/`__globals__` 链;白名单过滤就换成属性拼接(`getattr`、`[]`、`\x` 转义) |
| Python AST/正则黑名单(`eval` 内容被关键词拦) | 能提交表达式 | 用 `__builtins__` 索引、`().__class__.__base__.__subclasses__()`、字符串拼接绕过关键词 |
| Node `vm` / `vm2` | 能提交 JS | `this.constructor.constructor('return process')()`;`import`/`require` 受限时走 `process.mainModule` |
| 模板引擎(Jinja2/Twig/Freemarker/Velocity) | SSTI 点 | 从 `{{7*7}}` 确认,再找对象链读文件/执行;注意不同引擎的沙箱插件 |
| 容器/Docker 沙箱 | 有 shell 但被限 | 检查 capabilities(`capsh --print`)、可写挂载、`docker.sock`、宿主机路径挂载 |

## 纪律
- 先**确认沙箱边界**(能不能读文件?能不能联网?),再决定"逃逸"还是"就地取材"(很多题不需要真逃逸,读到目标文件即可);
- 用脚本把探测批量化(一次提交多个探针),不要一轮一轮手工试。
