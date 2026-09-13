# 取证分析 playbook

## 1. 流量分析(pcap/pcapng)——最高频
```bash
tshark -r x.pcap -q -z io,phs                     # 协议分层概览(先看这个!)
tshark -r x.pcap -Y "http.request" -T fields -e http.host -e http.request.uri | head -30
tshark -r x.pcap -Y "http.response" -T fields -e http.file_data | head -5
tshark -r x.pcap -Y "dns" -T fields -e dns.qry.name | sort -u
tshark -r x.pcap --export-objects http,outdir     # 导出所有 HTTP 对象
tshark -r x.pcap -Y "tcp contains \"flag\"" -x    # 直接搜关键字(最快的起手)
```
- **SQL 注入题**:看 `http.request.uri` 的 union/盲注;响应体里常带 base32/hex 分段的外带数据,提取后解码拼接。
- USB 流量:提取 HID(`-T fields -e usb.capdata`)还原键盘/鼠标。
- 无线:先找 WPA 握手包再爆破。

## 2. 磁盘 / 文件雕复
```bash
binwalk -e image.img            # 自动提取内嵌文件
foremost -i image.img -o out    # 按文件头雕复
fls -r image.img | head         # sleuthkit 列目录(必要时 mmls 找分区偏移)
icat image.img <inode> > f      # 按 inode 导出
testdisk image.img              # 分区/引导修复(交互)
```

## 3. 内存取证
```bash
vol -f mem.raw windows.info
vol -f mem.raw windows.pslist / windows.cmdline / windows.netscan
vol -f mem.raw windows.filescan | grep -i flag
vol -f mem.raw windows.dumpfiles --pid <PID>
vol -f mem.raw linux.bash
```
符号表可能需联网;失败时退回 `strings -a mem.raw | grep -i flag`。

## 4. 隐写
```bash
binwalk -e file                  # 有没有附加数据
exiftool file                    # 元数据/注释里常有 flag
steghide extract -sf file.jpg    # 先试空密码或题目提示词
zsteg -a file.png                # PNG LSB
strings -a file | grep -iE "flag|pass"
```
图片 IHDR 高度被改过→修复高度看隐藏区;GIF 逐帧;音频可用 python 画频谱找隐藏图案。

## 5. 文档 / 压缩包 / 日志
```bash
oleid file.doc ; oledump.py file.doc      # 宏文档
pdf-parser.py file.pdf | head
unzip -l a.zip; 7z l a.zip
zip2john a.zip > h.txt && john h.txt
```
ZIP 伪加密:把通用位标记改回 0 即可解压。
应急响应/日志:按时间排序、找异常 IP/用户名、找 `cmd=`/`wget`/base64 串。

## 6. 提速要点
- 先 `tshark -q -z io,phs` 看总览,再决定看哪层,别直接翻包。
- 关键字直搜比通读快:`-Y "tcp contains \"flag\""`。
- flag 常被分段编码藏在响应体:提出后写脚本解码;不确定编码类型时 `python3 tools/kb.py search "隐写 编码"`。

## flag 格式(实测踩过)
前缀**不固定**,见过 `NSSCTF{...}`、`flag{...}`、`LitCTF{...}` 等。提交前先看题目描述/容器 banner/回显/附件里有没有格式提示。
若第一次被拒,**优先检查前缀**再改内容;同一格式别重复提交(错误次数多会被判失败,靶场上限 20 次)。
