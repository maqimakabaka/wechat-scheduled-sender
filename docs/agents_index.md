# Agents 文档索引

> AI 在执行任务前先查本索引，快速定位所需文档。
> 维护要求：新增/修改文档后同步更新本表。

| 文档名称 | 用途说明 | 存储路径 | 最近修改时间 |
|---|---|---|---|
| README.md | 项目总说明：快速开始、目录结构、用户文件策略、功能、技术原理、环境坑、打包、验证记录、FAQ | `README.md` | 2026-11-03 |
| 设计文档-运营方案自动化.md | v1.2.0 内容编排/定时调度/线索接收/周报/合规校验的模块划分、设计取舍、踩过的坑、隐私与局限 | `docs/` | 2026-11-03 |
| 设计文档-设置与自动运行.md | v1.3.0 设置中心（28 项）、自动运行守护、发送时段、限额与冷却、自动清理的优先级依据、设计决定与踩过的坑 | `docs/` | 2026-11-03 |
| 设计文档-自检与容错.md | v1.4.0 健康自检、失败持久化与补发、备份恢复（含防路径穿越）、群管理降频/暂停的设计约束与验证记录 | `docs/` | 2026-11-03 |
| 设计文档-自带Python打包.md | v1.5.0 为什么内嵌 Python、embeddable vs PyInstaller 取舍、BOM/隔离模式两个坑、376→163MB 精简依据 | `docs/` | 2026-11-03 |
| **使用说明-给使用者.md** | **给最终使用者看的操作手册**（安装、首次使用、内容怎么改、自动运行、出问题先点自检、FAQ、合规红线）。用 `tools/md2docx.py` 转成 Word/PDF | `docs/` | 2026-11-04 |
| 排查记录-微信4.2.0.11控件树失效.md | 微信自动升级到 4.2.0.11 后控件树无法实体化的根因、证据与处置 | `docs/` | 2026-11-02 |
| 排查记录-Release混入CI残废包.md | Release 里多出 `WeChatSender-1.0.0-x64.exe` 的根因（tag 触发 CI）、为何那个包不能独立运行、清理与 workflow 修法 | `docs/` | 2026-11-04 |
| 设计文档-检查更新.md | v1.9.0 检查更新：为何不做静默自动更新、版本号从 `app.getVersion()` 传入、下载接口为何不接受 URL（防 SSRF） | `docs/` | 2026-11-04 |
| 排查记录-too many SQL variables.md | 大账号上联系人面板报「too many SQL variables」：`db.get_groups()` 用全量成员 id 拼 IN 超过 SQLite 参数上限，contacts.py 自行重写并分片 | `docs/` | 2026-11-04 |
| 排查记录-先发文字却没发文字.md | 「设置了先发文字却只发素材」：开始发送复用旧计划（text 为空）+ 编排模式 media_first 硬编码，含假引擎编排测试 | docs/ | 2026-11-05 |
| 设计文档-v2.0.0-截图开关与分发与微信目录定位.md | v2.0.0：截图存档开关、百度网盘分发入口、微信数据目录三层定位（含诊断与真实大账号等价性验证） | \docs/\ | 2026-11-05 |
| v1.8.0 使用教程片（非文档） | 宣传视频工程：成片 + 18 张脱敏界面截图 + 生成脚本。`宣传视频/` 已被 .gitignore 排除，不进仓库 | `宣传视频/1.8.0/` | 2026-11-04 |
| 制作说明.md | 宣传片规格、9 屏结构、设计取舍、工程复现方式 | `宣传视频/` | 2026-11-02 |
| 发布文案.md | 宣传片配套标题、短视频版/图文版正文、话题标签 | `宣传视频/` | 2026-11-02 |
| 定稿配音与时间轴.json | 各屏时长、标题、副标题、配音稿 | `宣传视频/` | 2026-11-02 |

## 代码结构速查

| 路径 | 职责 |
|---|---|
| `frontend/index.html` | 主界面：账号与目标、内容、素材、编排、计划、执行、线索、运维入口 |
| `frontend/settings.html` | **独立设置页**：30 项设置 + 自动运行 + 开机自启 + 备份恢复 |
| `frontend/common.css` | 两页共用的样式（卡片/表单/按钮/表格/toast） |
| `frontend/common.js` | 两页共用的脚本（环境、API、toast、转义、设置面板渲染） |
| `backend/campaign.py` | 内容编排：按星期引导语 + 原文案拼接 + 资料钩子 |
| `backend/scheduler.py` | 定时：绝对时间事件表、钩子延迟、每群每周上限 |
| `backend/inbox.py` | 接收：轮询数据库找出「谁主动私聊了我」，意图分类 |
| `backend/leads.py` | 线索表：登记去重、跟进状态、CSV 导出 |
| `backend/report.py` | 周报：投放日志、6 项 KPI 达成、砍群建议 |
| `backend/guard.py` | 合规：违规开票暗示拦截（带语境判断） |
| `backend/settings.py` | 配置中心：28 项设置的默认值、表单描述、校验与读写 |
| `backend/autorun.py` | 自动运行：判断「该不该跑 + 跑过没有」，到点触发（不依赖 server） |
| `backend/housekeeping.py` | 自动清理：按保留天数回收截图与投放日志 |
| `backend/doctor.py` | 健康自检：11 项依赖链检查，每项独立容错并给修法 |
| `backend/failures.py` | 失败登记与补发：按内容记账，成功自动销账 |
| `backend/backup.py` | 备份恢复：导入前自动快照 + 防路径穿越 |
| `backend/targets.py` | 群管理：active / reduced（隔天）/ paused |
| `tools/prepare-python.ps1` | 造出 vendor\python（打包所需的内嵌解释器），幂等可重跑 |
| `tools/md2docx.py` | Markdown 转排好版的 Word（给使用者的说明文档用） |
| `backend/media.py` | 素材：三种来源收集 + 按目标分配 |
| `backend/clipboard.py` | 剪贴板：图片 CF_DIB / 文件 CF_HDROP，供微信粘贴 |
| `electron/main.js` | 主进程：安装目录重定向、拉起/回收 Python 边车、建窗口 |
| `electron/preload.js` | 通过 contextBridge 把服务端口与 token 交给界面；暴露文件选择 |
| `backend/server.py` | 本地 HTTP 服务（token 鉴权 + CORS） |
| `backend/contacts.py` | 账号 / 群聊 / 好友 枚举 |
| `backend/sender.py` | 发送引擎（UIA 语义控件 + 截图 + 回读校验） |
| `backend/planner.py` | 发送计划生成（日期种子随机） |
| `backend/userdata.py` | 把第三方库的写入重定向到安装目录 |
| `frontend/index.html` | 控制台界面（单文件） |
| `launch.js` / `build.js` | Electron 启动器 / electron-builder 启动器（绕开 .bin shim） |

## 约定

- 临时文件放 `temp/`，不散落在项目其他位置
- `temp/`、`AI对话/` 已在 `.gitignore` 中，禁止提交
- 修改代码必须同步更新对应文档（至少更新本索引的「最近修改时间」）
