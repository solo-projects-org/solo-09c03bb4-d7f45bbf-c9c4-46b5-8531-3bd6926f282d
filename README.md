# 口述史馆 · 禁播消音标记与成片词表

口述史馆在一版短片里标出需要消音的内容后，剪辑师需要找到同一段原声在其他版本里的位置。
本系统导入各版剪辑表（CSV），审核员在任一版登记禁播时段（可跨剪接点），系统自动追溯到
原录音时段，并在任一版列出每次使用所需遮盖的成片时段；重叠处显示全部标记号。

字幕组另需一份可公开使用的**成片逐词词表**：按原录音号上传逐词稿，在剪辑版页面生成并
下载该版逐词 CSV，已按成片时间列出每个词的起止毫秒和文字，禁播词整词替换为〔消音〕。

## 运行

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库写入命名卷 `mute-data`（容器内 `/data/app.db`），重启后数据保留，
仍可按标记号核对剩余生效时段。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
DB_PATH=data/app.db uvicorn app.main:app --reload
python -m pytest tests/ -v     # 自测
```

## 概念与规则

- **剪辑版（version）**：一次导入的剪辑 CSV，导入后固定，不可修改、不可删除。
  每行 4 列：`原录音号, 源起点ms, 源终点ms, 成片起点ms`
  （表头 `recording_id,src_start_ms,src_end_ms,timeline_start_ms` 可带可不带）。
  片段按原速使用（成片时长 = 源时长），在成片时间线上互不重叠，导入时校验。
- **区间语义**：一律半开区间 `[起点, 终点)` —— 含起点、不含终点。
- **禁播标记（marker）**：登记在某一版的成片时间线上，可跨剪接点。登记时按该版
  剪辑表追溯到涉及的原录音时段并整笔存储（同一事务，要么全部写入要么不写）。
- **遮盖计算**：任一版中，标记剩余生效的原声时段经该版剪辑表映射回成片时间线，
  列出每次使用所需遮盖的时段；多个标记的遮盖重叠时，重叠段显示全部标记号。
- **整笔撤回**：撤回后该标记在所有版本立即不再产生遮盖（记录保留，可核对）。
- **局部解除（lift）**：在任一版按成片时段解除。解除时段换算到原声坐标、与该标记
  的原声时段求交后存储，因此作用于所有版本；其他标记照常生效。
  解除只追加新记录、不改写旧记录，且写事务串行化（`BEGIN IMMEDIATE`），
  两次并发解除不会互相覆盖。
- **逐词稿（transcript）**：按原录音号保存。每行 3 列：`原声起点ms, 原声终点ms, 词`
  （表头 `start_ms,end_ms,text` 可带可不带）。每行是一个独立的**整词单位**。
  换稿先完整校验，校验失败时旧稿原样保留；成功后修订号 +1，内容摘要（SHA-256）更新。
  内容相同的重传不改变摘要，不影响已生成词表。
- **成片词表（wordlist）**：为某一版生成一次性 CSV，首行
  `start_ms,end_ms,text`（UTF-8 BOM，便于表格软件打开）：
  - **整词落在同一剪辑片段内**才收录；跨剪接点的词整词不收。
  - 同一段原声在该版用了**几次就各列一次**（按片段逐一展开，按成片时间排序）。
  - 词的成片区间与该版**需遮盖的成片时段**有交集（半开区间）时，整词换成 `〔消音〕`，
    不做逐字切分。
  - 缺稿：列出所缺录音号，**暂停下载**；补稿后需重新生成。
  - 生成期间照常换稿、登记/撤回禁播标记（生成只取一次一致快照，不阻塞写入）。
  - **放行核对**：下载时重新比对（a）生成时所用各录音号的逐词稿摘要、（b）该版当前
    需遮盖的时段。任一不一致即整份不发出（409），生成记录标为失效并提示重试。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/versions` | 导入剪辑版 `{name, csv_text}` |
| GET | `/api/versions` / `/api/versions/{id}` | 版本列表 / 片段明细 |
| GET | `/api/versions/{id}/muted` | 该版当前需遮盖的成片时段（含标记号） |
| POST | `/api/markers` | 登记禁播标记 `{version_id, start_ms, end_ms, note}` |
| GET | `/api/markers` / `/api/markers/{id}` | 标记列表 / 详情（含剩余生效原声时段） |
| POST | `/api/markers/{id}/withdraw` | 整笔撤回 |
| POST | `/api/markers/{id}/lifts` | 局部解除 `{version_id, start_ms, end_ms}` |
| GET | `/api/markers/{id}/impact` | 该标记在每一版每次使用所需遮盖的时段 |
| PUT | `/api/transcripts/{recording_id}` | 上传 / 换稿 `{csv_text}`（失败保留旧稿） |
| GET | `/api/transcripts` / `/api/transcripts/{recording_id}` | 逐词稿列表 / 逐词明细 |
| POST | `/api/versions/{id}/wordlists` | 为该版生成词表（缺稿时 `status=missing_transcripts`） |
| GET | `/api/generations` / `/api/generations/{id}` | 生成记录列表 / 详情（含快照与失效原因） |
| GET | `/api/generations/{id}/download` | 放行核对通过后下载 CSV；否则 409 整份不发 |

交互式文档见 `/docs`。示例 CSV 见 `examples/`。

## 目录结构

```
app/
  main.py       FastAPI 入口与路由
  logic.py      领域逻辑（导入校验、追溯、解除、遮盖计算、逐词稿、成片词表）
  intervals.py  半开区间运算（并/减/扫描线合并）
  db.py         SQLite 连接与建表
  static/index.html  网页
tests/test_app.py       剪辑版/标记自测
tests/test_wordlist.py  逐词稿与成片词表自测
examples/               示例剪辑表与逐词稿
```
