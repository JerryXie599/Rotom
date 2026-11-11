# 卡:从指纹到 CVE 的最短路径(通用)

**指纹**:已经知道组件名/端口/大概版本,但不知道"这个版本有什么洞、怎么验"。**任何组件都先走这张卡。**

## 最短路径

```bash
# 1) 把版本拿到手(优先级从上到下)
curl -s -D- -o /dev/null http://T/            # 响应头(Server/X-Powered-By/自定义版本头)
curl -s  http://T/api/version http://T/system_stats http://T/api/tags 2>/dev/null   # 很多服务自带版本接口
curl -s  http://T/ | grep -oE '"[0-9]+\.[0-9]+\.[0-9]+"'      # 前端 bundle 里的版本/构建号
curl -s  http://T/CHANGELOG.md http://T/package.json http://T/version 2>/dev/null
# 容器里能执行命令时(拿到 RCE/命令注入/调试端口后):直接读组件自己的版本文件
#   python:  pip show <pkg> / <pkg>/__init__.py 里的 __version__
#   前端:    <app>/package.json  |  node:  <app>/node_modules/<pkg>/package.json
#   系统包:  dpkg -l | grep <pkg> | rpm -qa | grep <pkg>
```

```bash
# 2) 找这个版本的公开通告(按信息量排序)
#    a. 厂商/GHSA 安全公告(最准,常带"影响版本 + 修复版本 + 复现")
#       GitHub 仓库 → Security → Advisories;或搜 "<组件> security advisory <版本>"
#    b. 上游仓库的修复提交/PR(最有用,见第 3 步)
#    c. NVD/CVE 库搜组件名(用来补 CVE 号与版本区间)
```

```bash
# 3) **把上游修复 PR 的测试文件当验证清单**(实测最快)
#    修复一个漏洞的 PR 通常自带:安全测试(pytest/单元测试)+ 新增的路径过滤/白名单代码。
#    读这两样就等于拿到"这个洞怎么打、打在哪个路由、什么条件下成立":
#      - 测试里的函数名/路由 → 直接告诉你攻击面(payload 形状)
#      - 补丁里加的校验 → 反过来告诉你**没校验前**能穿到哪(利用点)
#    例:目标 = ComfyUI,读 GHSA-779p-m5rp-r4h4 的补丁(PR#14734)拿到
#        tests-unit/security_test/test_ghsa_779p_03_annotated_traversal.py 这类文件,
#        一眼看出"LoadImage 的 image 参数只做 os.path.exists 校验" → 直接构造穿越。
```

## 判定与打法

1. **版本比对**:公告会给"影响范围(< X.Y.Z)"。目标版本落进去 → 才值得打;没落进去别浪费时间。
2. **路由确认**:公告/补丁里提到的路由必须在目标上**真实存在**(curl 一下看是否 200/400 而不是 404)——
   很多组件同一版本有编译期开关,路由可能没开。
3. **一次只验一个**:先把"最小可复现请求"发出去(看回显/状态码差异),成立再加后续利用链。
4. **成立就写黑板**:把"组件+版本+路由+最小 payload"一行写进黑板,队友不用重查。

## 边界与常见坑

- **别盲打 nuclei 全模板**:按组件筛模板(`nuclei -u http://T/ -t http/cves/<vendor>/ -rl 10`),否则噪音吞掉时间。
- **老 CVE 往往比新 CVE 好打**:内网/靶场里组件版本普遍偏旧,先试 >=3 年前的经典洞。
- **同族组件差别很大**:例如 ComfyUI 的自定义节点生态、Grafana 的插件目录、Jenkins 的插件路由 ——
  公告里的"组件"常指主程序,插件/扩展的问题要看它自己的版本。
- **拿不到版本时**:先用行为指纹(响应特征、报错文案、静态资源路径、默认页面结构)缩小到"哪几年"的版本,
  再按"那一代的经典洞"逐个试;别停在"不知道版本所以打不了"。
- **框架的"设计内后门"**:有些组件默认无鉴权(Jupyter 未设 token、Docker API 2375、Railway/etcd 未授权),
  这类不需要 CVE 号 —— 直接按"未授权访问"处理。