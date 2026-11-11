# Rotom 托管运行镜像(TSec Benchmark 托管模式)
#
# 目标:平台起容器后**自己开始解题**,全程不依赖公网(除平台白名单里的模型网关 + 答题 API)。
# 构建(在能上网的机器上做,运行环境不需要网):
#   bash docker/build.sh                # → dist/rotom-agent.tar.gz
#
# 不会进镜像的东西见仓库根目录的 .dockerignore:运行数据(logs/work/projects)、历史战果(kb_extra)、
# 明文凭据(.env)、测试夹具(tests/,里面有 mock 平台用的假 flag)。构建末尾还会跑一次
# docker/selfcheck.sh 审计:发现"历史答题记忆 / 答案样式 / 密钥"就直接构建失败。
# 基础镜像:官方 python:3.12-slim-bookworm。默认走 daocloud 的官方镜像副本 ——
# 本机/国内网络访问 Docker Hub 不通(实测 registry-1.docker.io 超时),该副本内容一致。
# 需要时用 --build-arg BASE_IMAGE=python:3.12-slim-bookworm 切回官方源。
ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim-bookworm

# ================================================================
# 阶段 1(appsrc):把仓库拷进来 → **在这里脱敏** → 得到一棵干净的应用树。
# 为什么单独一个阶段:如果在最终镜像里"先 COPY 再覆盖脱敏",敏感文本仍留在底层 layer 里
# (docker save 出来的 tar 能扒出来)。多阶段只有 **最终阶段** 的层会随镜像发布,
# 所以原始文本不会出现在交付物里(实测:导出后用 grep 逐层核过)。
# ================================================================
FROM ${BASE_IMAGE} AS appsrc
WORKDIR /app
COPY . /app
RUN bash docker/sanitize.sh \
    && rm -f docker/sanitize.sh \
    && python3 tools/kb.py reindex

# ================================================================
# 阶段 2(最终镜像):工具链 + 离线资料 + 从阶段 1 拷进来的干净应用树
# ================================================================
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1 PYTHONUTF8=1 \
    ROTOM_HOSTED=1 \
    PATH=/app/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ---------------------------------------------------------------- 下载源(构建期)
# 本机访问 Docker Hub 不通、deb.debian.org 会卡住 → 构建期统一走国内镜像。
# **这只是"从哪儿下载"**,装的是同一批官方包;运行期完全不联网(见 docs/DOCKER.md 无外网审计)。
RUN set -eux; \
    rm -f /etc/apt/sources.list.d/debian.sources; \
    printf '%s\n' \
      'deb http://mirrors.tencent.com/debian bookworm main' \
      'deb http://mirrors.tencent.com/debian-security bookworm-security main' \
      'deb http://mirrors.aliyun.com/debian bookworm main' \
      'deb http://mirrors.aliyun.com/debian-security bookworm-security main' \
      'deb http://mirrors.tuna.tsinghua.edu.cn/debian bookworm main' \
      'deb http://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main' \
      > /etc/apt/sources.list; \
    printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "8";\nAcquire::http::Timeout "45";\nAcquire::https::Timeout "45";\n' \
      > /etc/apt/apt.conf.d/99-rotom-retry; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl wget git jq unzip zip xz-utils less vim-tiny file \
        procps psmisc tini gnupg \
        socat netcat-openbsd telnet dnsutils iproute2 iputils-ping net-tools iptables openvpn \
        gcc g++ make python3-dev libssl-dev libffi-dev pkg-config \
        nmap whatweb sqlmap gobuster ffuf masscan hydra dirb smbclient ldap-utils redis-tools \
        default-mysql-client postgresql-client sqlite3 \
        gdb binutils strace ltrace patchelf \
        tcpdump tshark binwalk libimage-exiftool-perl steghide foremost sleuthkit \
        hashcat john ripgrep \
        perl libnet-ssleay-perl libio-socket-ssl-perl libxml-writer-perl \
        openjdk-17-jdk-headless php-cli \
    && rm -rf /var/lib/apt/lists/* \
    # ---- 交付物洁净:删掉 apt 装的上游文档/示例(里面含示意的用户主目录路径) ----
    # 必须在**同一层**里删:换个层再删只是"盖住",原字节仍留在底层 layer(docker save 能扒出来,实测过)
    && rm -rf /usr/share/doc/* /usr/share/man/* /usr/share/info/* \
    && find /usr/lib/ruby -maxdepth 6 -iname 'README*' -delete 2>/dev/null || true \
    # nmap 自带的"默认账号指纹表/NSE 测试样例"里有示例内网 IP —— 就地换成占位符(同层,功能不变)
    && sed -i -E 's/192\.168\.[0-9]+\.[0-9]+/<LAN_IP>/g' \
             /usr/share/nmap/nselib/*.lua /usr/share/nmap/nselib/data/*.lua /usr/share/nmap/scripts/*.nse \
             /usr/share/hydra/*.csv 2>/dev/null || true \
    # perl/ruby/exiftool 自带的**文档与示例**里有示意的用户主目录路径 —— 同层删/替换(实测:就这 8 个文件)
    && rm -f /usr/lib/ruby/gems/*/gems/debug-*/CONTRIBUTING.md \
             /usr/lib/ruby/gems/*/gems/rake-*/doc/rakefile.rdoc \
             /usr/share/rubygems-integration/all/gems/rake-*/doc/rakefile.rdoc 2>/dev/null || true \
    && find /usr/bin /usr/share/perl5 /usr/share/perl /usr/lib/ruby /usr/share/rubygems-integration \
             -maxdepth 7 -type f -size -5M \
             -exec grep -lF -m1 /Users/ {} + 2>/dev/null \
             | xargs -r sed -i -E 's#/Users/[A-Za-z0-9._-]*#<用户目录>#g' 2>/dev/null || true \
    # apt 索引也要在**这一层**里删干净(否则索引文件里的示例路径会留在中间层;实测 apt 列表里有)
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- Python 库(解题常用)
# 多索引兜底:单一镜像偶发 SSL 断流(实测 UNEXPECTED_EOF_WHILE_READING),pip 会依次尝试
RUN pip install --no-cache-dir --retries 5 --timeout 60 \
        -i http://mirrors.tencent.com/pypi/simple/ --trusted-host mirrors.tencent.com \
        --extra-index-url https://mirrors.aliyun.com/pypi/simple/ \
        --extra-index-url https://pypi.org/simple/ \
        requests pwntools pycryptodome sympy z3-solver pyjwt \
        beautifulsoup4 lxml dnspython numpy openpyxl pillow gmpy2 \
        dirsearch pyelftools psutil tqdm colorama

ARG NODE_VERSION=v22.19.0
ARG PI_VERSION=0.87.1

# ---------------------------------------------------------------- Node + pi(模型 CLI)
# Node 二进制走 npmmirror 的官方镜像副本(nodejs.org 在国内常卡住,实测 14 分钟没动);
# npm 也走 npmmirror,pi 版本钉死。
RUN set -eux; \
    ( curl -4 -fsSL --connect-timeout 20 --retry 3 --retry-delay 2 \
        "https://cdn.npmmirror.com/binaries/node/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
      || curl -4 -fsSL --connect-timeout 20 --retry 3 --retry-delay 2 \
        "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz ); \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \
    rm /tmp/node.tar.xz; \
    npm config set registry https://registry.npmmirror.com; \
    npm config set fetch-retries 5; \
    npm install -g --omit=dev "@earendil-works/pi-coding-agent@${PI_VERSION}"; \
    npm cache clean --force; \
    # node/pi/npm 自带的文档与示例里也有示意的用户主目录路径 —— 同层删文档、替换示例(不影响运行)
    find /usr/local/lib/node_modules -type f -name '*.md' -delete 2>/dev/null || true; \
    find /usr/local/lib/node_modules -type f -size -3M \
         \( -name '*.js' -o -name '*.ts' -o -name '*.d.ts' -o -name '*.json' -o -name '*.py' -o -name '*.pyc' \) \
         -exec grep -lF -m1 /Users/ {} + 2>/dev/null \
         | xargs -r sed -i -E 's#/Users/[A-Za-z0-9._-]*#<用户目录>#g' 2>/dev/null || true; \
    pi --version

# ---------------------------------------------------------------- 安全工具(GitHub release;构建期下载)
ARG NUCLEI_VERSION=3.11.1
ARG R2_VERSION=6.2.2
ARG NIKTO_TAG=2.6.1

# Debian bookworm 里没有 nikto / radare2(已从 Debian 撤包)→ 用各自官方渠道补上。
# 版本都钉死,便于审计;下载失败只告警,selfcheck 会点出来。
RUN set -eux; \
    curl -4 -fsSL --connect-timeout 20 --retry 3 --retry-delay 2 -o /tmp/nuclei.zip "https://github.com/projectdiscovery/nuclei/releases/download/v${NUCLEI_VERSION}/nuclei_${NUCLEI_VERSION}_linux_amd64.zip" \
      && unzip -q /tmp/nuclei.zip -d /usr/local/bin nuclei && chmod +x /usr/local/bin/nuclei && rm /tmp/nuclei.zip \
      || echo "⚠ nuclei 下载失败(不阻塞构建)"; \
    curl -4 -fsSL --connect-timeout 20 --retry 3 --retry-delay 2 -o /tmp/r2.deb "https://github.com/radareorg/radare2/releases/download/${R2_VERSION}/radare2_${R2_VERSION}_amd64.deb" \
      && apt-get install -y --no-install-recommends /tmp/r2.deb && rm -f /tmp/r2.deb \
      || echo "⚠ radare2 安装失败(不阻塞构建)"; \
    mkdir -p /opt/nikto && curl -4 -fsSL --connect-timeout 20 --retry 3 --retry-delay 2 -o /tmp/nikto.tar.gz "https://github.com/sullo/nikto/archive/refs/tags/${NIKTO_TAG}.tar.gz" \
      && tar -xzf /tmp/nikto.tar.gz -C /opt/nikto --strip-components=1 \
      && printf '#!/bin/sh\nexec perl /opt/nikto/program/nikto.pl "$@"\n' > /usr/local/bin/nikto \
      && chmod +x /usr/local/bin/nikto && rm -f /tmp/nikto.tar.gz \
      || echo "⚠ nikto 安装失败(不阻塞构建)"; \
    mkdir -p /usr/local/share/tools; \
    curl -4 -fsSL --connect-timeout 20 --retry 3 --retry-delay 2 -o /usr/local/share/tools/ysoserial-all.jar \
        "https://github.com/frohoff/ysoserial/releases/download/v0.0.6/ysoserial-all.jar" \
      || echo "⚠ ysoserial 下载失败(不阻塞构建)"; \
    curl -4 -fsSL --connect-timeout 20 --retry 3 --retry-delay 2 -o /usr/local/share/tools/jdwp-shellifier.py \
        "https://raw.githubusercontent.com/IOActive/jdwp-shellifier/master/jdwp-shellifier.py" \
      || echo "⚠ jdwp-shellifier 下载失败(不阻塞构建)"

# 常用字典(rockyou 可选:下载失败不阻塞;apt 的 wordlists 已含 dirb/dirbuster 列表)
RUN set -eux; \
    mkdir -p /usr/share/wordlists; \
    curl -4 -fsSL --connect-timeout 20 --retry 3 --retry-delay 2 -o /usr/share/wordlists/rockyou.txt.gz \
        "https://raw.githubusercontent.com/danielmiessler/SecLists/master/Passwords/Leaked-Databases/rockyou.txt.tar.gz" \
      && (cd /usr/share/wordlists && tar -xzf rockyou.txt.gz && rm -f rockyou.txt.gz) \
      || echo "⚠ rockyou 下载失败(不阻塞;用 dirb 字典即可)"; \
    mkdir -p /usr/share/wordlists/seclists; \
    for f in Discovery/Web-Content/common.txt Discovery/Web-Content/raft-small-words.txt \
             Discovery/Web-Content/directory-list-2.3-medium.txt Discovery/DNS/subdomains-top1million-5000.txt \
             Passwords/Common-Credentials/10-million-password-list-top-1000.txt; do \
      out="/usr/share/wordlists/seclists/$(basename "$f")"; \
      curl -4 -fsSL --connect-timeout 20 --retry 3 --retry-delay 2 -o "$out" \
        "https://raw.githubusercontent.com/danielmiessler/SecLists/master/$f" || rm -f "$out"; \
    done; \
    ls -la /usr/share/wordlists/seclists | head

# nuclei 模板(nuclei 默认读 ~/nuclei-templates;由 docker/prepare_context.sh 预置进构建上下文)
# ⚠ 必须从**已脱敏的 appsrc 树**拷,不能直接从构建上下文拷 —— 宿主机的模板目录里带着:
#   ① nuclei 生成的索引缓存(.checksum/.templates-index,记录模板的绝对来源路径,实测 2.7 万处 /Users/…);
#   ② 上游模板自带的示例串(宿主机路径、行业系统产品名等)。
# 两者都在阶段 1 被 sanitize.sh 就地清掉;直接从上下文拷会把这些原件带进最终镜像(2026-10-19 逐层审计抓到)。
COPY --from=appsrc /app/knowledge/vendor/nuclei-templates/ /root/nuclei-templates/

# ---------------------------------------------------------------- 项目本体
WORKDIR /app
# 只拷"已脱敏"的应用树(见阶段 1);原始文本从不进入最终镜像的任何层
COPY --from=appsrc /app /app

# 登录 shell 会重置 PATH(bash -lc 走 /etc/profile)—— 把 tools/bin 写进 profile.d 与 .bashrc,
# 否则 agent 按老写法调 `orb ...` 时会"找不到命令"(实测踩到;macOS 版当年也踩过同一个坑)。
RUN printf 'export PATH=/app/tools/bin:$PATH\n' > /etc/profile.d/rotom-path.sh \
    && chmod +x /etc/profile.d/rotom-path.sh \
    && printf 'export PATH=/app/tools/bin:$PATH\n' >> /root/.bashrc

# 知识库索引(容器内路径:/app,重建一次即可),顺手做构建期审计
RUN python3 tools/kb.py reindex \
    && chmod +x docker/*.sh tools/bin/* start.sh \
    && bash docker/selfcheck.sh --build

ENV ROTOM_RUN_DIR=/app
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]