# 卡:AI / ML 服务 CVE 与打法速查

**指纹**:端口(8188/7860/11434/5000/8080/3000/8000/8265/3001)、前端标题、`/docs` `/openapi.json` 可达、
响应里出现模型/工作流/节点/知识库等词。众测里这类组件越来越多,而且**普遍默认无鉴权**。

> 拿不准的地方只写方法、不编号(编错号比不写更坏)。带 CVE 号的条目都是可公开检索的通告,
> **先用版本确认再套用**;版本从哪读见 [find-cve.md](find-cve.md)。

## 通用打法(先做这三步,顺序别反)

1. **先要"任意读"再要"任意写"**:这类服务几乎都有"用户可控的文件路径/URL/模板"入口,
   能读文件就能直接读 flag;命令执行是备选,不是首选。
2. **枚举它自己的 API**:`/docs`、`/openapi.json`、`/api/*`、`/internal/*`、`/manager/*` ——
   把端点列全比逐个猜快得多;特别注意 `install/upload/import/webhook/run/pull` 这类动词端点。
3. **看它的插件/自定义扩展目录**:自定义节点、插件、工作流模板往往**没有和主程序同等强度**的校验,
   是穿越/任意写/RCE 的高发区。

## ComfyUI(8188,`/system_stats` 返回版本)

- 版本:`GET /system_stats` → `comfyui_version`(实测可直接拿到)。老版本(< 0.28.0)有一组公开通告
  **GHSA-779p-m5rp-r4h4**(2026-07 的上游 PR 修复了 4 个):`/view` 的 SVG stored XSS、
  `/experiment/models/preview` 路径穿越、`/userdata` 的 content-type XSS、
  以及 **`LoadImage` 经 `/prompt` 的路径穿越**。
- **最短路径(路径穿越,实测有效)**:
  ```bash
  # LoadImage.image 只做 os.path.exists 校验 → 可让 PIL 去打开任意路径
  # 文件不存在时校验报 "Invalid image file" —— 顺带就是一个**文件存在性 oracle**:
  #   先枚举常见 flag 落点(/flag.txt、/challenge/flag.txt、/app 下、用户目录),再读存在的那个
  curl -s -X POST http://T/prompt -H 'Content-Type: application/json' \
    -d '{"prompt":{"1":{"class_type":"LoadImage","inputs":{"image":"../../../../flag.txt"}}}}'
  ```
  变成"读出内容"的两条路:① `LoadImageTextDataSetFromFolder` 之类的数据集节点会把目录里 `*.txt`
  经 `PreviewAny` 回显;② 用 `/upload/image` + `subfolder`、`save_images_to_folder` 的
  `folder_name`/`filename_prefix`(老版本未净化)做"任意目录写 .txt/.png"再回读。
- **Manager 安装链**(装了 ComfyUI-Manager 时):`/customnode/install/pip`、`/manager/queue/install_model`
  这类端点常把关不严(如 install_model 只校验 filename/save_path 而 **url 不校验** → 可下任意内容)。
  ⚠ **别用会阻塞目标事件循环的同步调用**(如 `install/git_url` 触发的同步 `git clone`):
  实测会把单线程服务**永久卡死**(先 refused、后 TCP 通但永不回 HTTP),平台状态却仍显示容器可用。

## 其它常见 AI/编排服务(方法级,先按通用三步走)

| 组件 | 指纹 | 打法 |
|---|---|---|
| Ollama | 11434,`/api/tags` 列出本地模型 | 默认无鉴权 → `/api/pull` 拉模型、`/api/delete` 删模型、`/api/show` 读 Modelfile(可能含凭据);新版才有 key 机制,先探 `/api/tags` |
| Gradio | 7860,页面 `/config` 返回 JSON | 老版本接口通过 `POST /run/predict` / `/api/predict` 暴露任意函数 → 找"文件路径/URL 参数"的函数(上传、下载、加载数据集);先拉 `/config` 看函数签名 |
| MLflow | 5000,`/api/2.0/mlflow/experiments/search` | 无鉴权 → 列举实验/artifact;模型注册表的 `source` 可指向任意 URI(`http(s)/s3/`)→ 指向自控的 pickle 即为反序列化 RCE |
| Ray Dashboard | 8265,`/api/version` | 无鉴权 → `POST /api/jobs/` 提交 job(指定 entrypoint/working_dir)直接执行命令 |
| Langflow / Flowise / n8n / Dify / AnythingLLM | 3000/3001/5678/8080,前端标题 | "工作流/代码节点"型:找 `code/execute/run` 类端点 → 节点里写代码即 RCE;没有命令执行时找"自定义 API 工具"的 URL 参数做 SSRF/任意读 |
| Jupyter / Notebook | 8888,`/tree` | 未设 token 直接开 terminal;若挂了反向代理,试 `/api/contents/` 读任意文件 |
| vLLM / TGI / FastChat | 8000/8080,`/v1/models` | OpenAI 兼容接口;关注 `/v1/chat/completions` 的 prompt 注入与"工具调用"外发 URL |
| Weaviate / Qdrant / Chroma | 8080/6333/8000,`/v1/schema`、`/collections` | 向量库默认无鉴权 → 列举集合、读出全部文档(常含知识库原文/凭据) |

## 边界与注意

- **先读再写再执行**:读文件类原语副作用最小、最稳;命令执行类容易被平台/容器限制(无外网、无 writable 目录)。
- **容器"无外网"是常态**:需要回连/下载的利用链会失败 —— 优先**带内回显**(把结果写在响应里)或"读文件",
  不要先假设自己能出网。
- **别把目标打死**:同步阻塞型调用、大批量并发请求都可能让单线程服务进入死锁/假死;
  出现"TCP 通但不回 HTTP"时,别在应用层反复重试,应让 harness 换容器。
- **数据目录很值钱**:`user/`、`data/`、`uploads/`、`outputs/`、`custom_nodes/`、`models/` 这类目录里
  既有 flag,也常有 token/凭据 —— 拿到任意读后先扫这些。
---

## CVE 表:AI/ML 组件(含 2026-09-29 那轮实测/检索涉及的条目)

**状态图例**:`✅实测` = 在真实目标上看到过路由/版本/验证迹象(含上游公告与补丁原文);`📋公告` = 公开通告里的经典条目;
`⚠候选` = 本轮检索上下文里出现、但**未在上游公告上核实**,用前必须核对(见文末"候选待核")。

### ComfyUI(核心)

| CVE | 影响 | 路由/触发 | 验证方法 | 状态 |
|---|---|---|---|---|
| CVE-2026-56670 | Stored XSS(SVG 上传) | `GET /view` | 上传含脚本的 SVG → 用 `/view` 内联打开看是否执行(修法=强制下载 SVG/XML) | ✅实测 |
| CVE-2026-56671 | 路径穿越 | `GET /experiment/models/preview` | `preview?filename=../../../../etc/passwd` 类读文件;修法=限制在 model 目录内 | ✅实测 |
| CVE-2026-56672 | Stored XSS(content-type 嗅探) | `GET /userdata/{file}` | 上传 html 类文件 → `/userdata/...` 内联渲染看是否执行 | ✅实测 |
| CVE-2026-56673 | **路径穿越(最有用的读文件原语)** | `POST /prompt` 的 `LoadImage.image` | `image="../../../../flag.txt"` 能过校验(`exists_annotated_filepath` 只做 `os.path.exists`);**文件不存在时 `VALIDATE_INPUTS` 报 `Invalid image file` → 同时是文件存在性 oracle**;修法=加固 `get_annotated_filepath` | ✅实测 |
| CVE-2026-56674 | Origin: null CSRF | 跨站表单 | **实测被既有 `Sec-Fetch-Site: cross-site` 检查挡住**,不可利用 | ✅实测(不可用) |

> 这一族对应公告 **GHSA-779p-m5rp-r4h4**(4 个已修 + 1 个判为已缓解),上游在 **0.28.0 / PR#14734(2026-07-15)** 一次性修完;
> 版本 < 0.28.0 全部受影响。**拿版本的正规途径**:`GET /system_stats` → `comfyui_version`。

### ComfyUI-Manager(装了 Manager 时)

| CVE | 影响 | 说明 | 状态 |
|---|---|---|---|
| CVE-2024-21574 / 21575 / 21576 | 路径穿越/任意文件类(历史条目) | 与核心的 `/userdata`、`/view` 系列同源;**先看 Manager 自己的版本**(`ComfyUI-Manager` 目录里的版本号) | 📋公告 |
| CVE-2025-6730 / 2025-6104 / 2025-44564 / 2025-23995 / 2024-10481 / 2024-10099 / 2024-4760 / 2024-21535 | ComfyUI/Manager 历史条目 | 本条来自本轮检索上下文,**条目名与版本区间未核实** | ⚠候选 |

> **Manager 的安装链本身就是攻击面**(不需要 CVE 号):`POST /customnode/install/pip`(`{"cmd":"install <本地包>"}`)、
> `POST /manager/queue/install_model`(url 字段不校验 → 下载任意内容)、`git_exe` 可指向命令、`/api/userdata` 可上传任意文件。
> ⚠ **别用 `install/git_url` 触发同步 git clone** —— 实测会把单线程事件循环阻塞死(容器假死,平台仍报 available)。

### Langflow

| CVE | 影响 | 路由/触发 | 验证方法 | 状态 |
|---|---|---|---|---|
| CVE-2025-3248 | **未授权 RCE**(< 1.3.0) | `POST /api/v1/validate/code` | body 里放带 `@exec` 装饰器的函数体 → 回显执行结果;拿 flag 常靠它读容器内文件 | ✅实测 |
| CVE-2024-37014 | 任意文件读 | `/api/v1/files/...` 等 | 沿 `files` 路由做穿越/直读 | ✅实测 |
| CVE-2025-57760 | 权限提升 | — | 检索上下文出现,未核实 | ⚠候选 |

### Gradio

| CVE | 影响 | 路由/触发 | 状态 |
|---|---|---|---|
| CVE-2024-1561 | 本地文件读 | `GET /file=<path>`(≤ 4.12.x) | 📋公告 |
| CVE-2023-51449 | LFI | `/file=` 路径穿越(更早版本) | 📋公告 |
| CVE-2024-1726 | 未核实 | — | ⚠候选 |

> Gradio 的通用入口:先拉 `GET /config` 拿到所有函数签名,找**带文件路径/URL 参数**的 `/run/predict` 或 `/api/predict` 函数,
> 这类老版本几乎不校验路径。

### Dify

| CVE | 影响 | 说明 | 状态 |
|---|---|---|---|
| CVE-2025-55182 | "React2Shell" 类 | 目标为 Dify Next.js 前端(3000)+ 内部 API(5001);本轮按此方向探测 | ✅实测(探测过) |
| 未授权 API | 设计内风险 | API 前缀常在 `http://<host>:5001/v1/...`,老版本默认无鉴权 | 📋公告 |

### AI 栈依赖(推理/加载链上容易被忽略的闸门)

| CVE | 影响 | 与本题材的关系 | 状态 |
|---|---|---|---|
| CVE-2025-32434 | `torch.load(..., weights_only=True)` 仍可 RCE(≤ 2.5.1,修于 2.6.0) | 判断"能不能用 pickle 反序列化"时**先看 torch 版本**;新版(≥2.6)这条路直接关掉 | ✅实测(版本不适用) |
| CVE-2023-4863 | libwebp 堆溢出(经 Pillow 触发) | "让 PIL 打开任意文件"类原语很难升级成 RCE,通常**只用它做读文件/oracle** | 📋公告 |
| CVE-2024-31580 / 31583 | PyTorch 历史条目 | 未核实 | ⚠候选 |

### 候选待核(本轮检索里出现、但没在上游公告核对过的条目名)

`CVE-2024-10099` · `CVE-2024-10481` · `CVE-2024-21535` · `CVE-2024-21574` · `CVE-2024-21575` · `CVE-2024-21576` ·
`CVE-2024-31580` · `CVE-2024-31583` · `CVE-2024-4760` · `CVE-2025-23995` · `CVE-2025-44564` · `CVE-2025-6104` · `CVE-2025-6730` ·
`CVE-2024-1726` · `CVE-2025-57760`

**处理方式**:先用 `find-cve.md` 的流程核对"组件 + 版本区间 + 路由",核对不上就**不要打** ——
编造的 CVE 比没有 CVE 更浪费时间。
