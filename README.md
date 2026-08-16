# course-data-prep

政大課程資料的準備流程。產出一份 PostgreSQL 成品(`.sql.gz`)給
[CourseLangChain](https://github.com/NCCUCourseScheduling/CourseLangGraph)(查詢端)使用。

**這個 repo 自己帶一個 PostgreSQL**(`docker compose`,對外 port **5433**),原始課程資料與
所有中間狀態都留在這裡。查詢端只收最後產出的 `.sql.gz` 成品 —— 未處理的原始資料不該進到那個
純執行期的 repo。

用的是與查詢端相同的 `paradedb/paradedb` image(內建 `pg_search` + `pgvector`,都預編好),
所以兩邊擴充版本一致,而且都不需要自己編 Dockerfile。5433 與查詢端的 5432 並存不打架。

> 查詢端的安裝與執行看 [`CourseLangChain/docs/SETUP.md`](../CourseLangChain/docs/SETUP.md)。
> **這份文件負責的是「資料準備」階段** —— 每學期做一次,由資料維護者執行。
> 只是要把 app 跑起來、而且已經有別人給的 `.sql.gz`,你不需要這個 repo。

## 為什麼要拆成獨立 repo

embedding 是整條流程裡唯一真的吃 GPU 的步驟,而 Docker Desktop 在 macOS 沒有 GPU
passthrough、Linux 要 `nvidia-container-toolkit` —— 放在容器裡跑等於強制用 CPU。搬到
host 用 `uv` 跑才拿得到 CUDA / MPS。

順帶的好處:查詢端變成純執行期,一般開發者拿到成品就能跑,不用 GPU、不用下載模型。

## 需求

- `python >= 3.13` 與 [`uv`](https://github.com/astral-sh/uv)
- Docker(這個 repo 的 compose 會起一個 PostgreSQL,port 5433)
- 爬蟲產出的 SQLite(`NCCUCourse` 的 `.db`,不進 git)
- 兩個 repo 放成兄弟目錄:

```
CourseLangChain-agents/
├── CourseLangChain/       ← 查詢端(含 docker compose)
└── course-data-prep/      ← 這裡
```

---

# 逐步流程

## 1. 起資料庫

```sh
docker compose up -d postgres
```

## 2. 把爬蟲的 SQLite 灌進去

[NCCUCourse](https://github.com/NCCUCourseScheduling/NCCUCourse) 爬出來的是 SQLite,
後面每一步都是 PostgreSQL。pgloader 直接把資料搬進上一步那個資料庫:

```sh
LOAD_SQLITE=<爬蟲產出>.db docker compose --profile load run --rm pgloader
#           table name     errors       rows
#  ---------------------  ---------  ---------
#          public.course          0       3635
#            public.rate          0     232652
#          public.result          0       1308
#         public.teacher          0         11
```

接著把整數欄位收窄 —— SQLite 的 INTEGER 沒有寬度,pgloader 一律當 `bigint` 搬:

```sh
docker compose exec postgres psql -U postgres -d academic -c "
DO \$\$
DECLARE r RECORD;
BEGIN
    FOR r IN (SELECT table_name, column_name FROM information_schema.columns
              WHERE table_schema='public' AND data_type='bigint')
    LOOP
        EXECUTE format('ALTER TABLE %I ALTER COLUMN %I TYPE integer', r.table_name, r.column_name);
    END LOOP;
END;
\$\$;"
```

確認:

```sh
docker compose exec postgres psql -U postgres -d academic \
  -c "SELECT y, s, COUNT(*) FROM course GROUP BY y,s ORDER BY y DESC LIMIT 3;"
#   y  | s | count
#  115 | 1 |  2923     ← contract.yaml 指定的學期
```

pgloader 順手處理掉兩件事:表名 `COURSE` → `public.course`(下游都寫 `FROM course`),
以及依 SQLite schema 建出主鍵。

> [!IMPORTANT]
> pgloader 是**建表**,所以重灌前要先清掉舊的:
> ```sh
> docker compose exec postgres psql -U postgres -d academic \
>   -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
> ```
> 另外它遇到約束衝突只會記一行 `ERROR` 然後**照樣 exit 0** —— 例如主鍵欄位有 NULL 時,
> 整個 PRIMARY KEY 會被默默丟掉。輸出裡出現 `ERROR` 就不要往下走。

## 3. 安裝

```sh
uv sync
```

`DATABASE_URL` 不用設,預設就是這個 repo 自己的資料庫
(`postgresql://postgres:postgres@localhost:5433/academic`)。

## 4. 套 schema

建向量欄位、BM25 索引(`pdb.jieba` 分詞)、結構化時間欄位,並把契約寫進 `schema_meta`:

```sh
uv run python -m prep.migrate
# [migrate] 套用 02-extensions.sql ...
# [migrate] 套用 03-schema.sql ...
# [migrate] 套用 04-time-columns.sql ...
# [migrate] 完成。schema_meta:
#            schema_version = 6
#            embed_model = BAAI/bge-base-zh-v1.5
```

冪等,可以重複執行。

## 5. 解析上課時間

把 `time_raw`(如 `五234`)拆成 `sessions` / `weekdays` / `has_morning` 等欄位:

```sh
uv run python -m prep.populate_time
# [populate] 101883/101883
# [populate] 完成,共更新 101883 列。
```

抽查:

```sh
docker compose exec postgres psql -U postgres -d academic \
  -c "SELECT time_raw, sessions FROM course WHERE time_raw='五234' LIMIT 1;"
#  五234 | [{"weekday": 5, "end_hour": 12, "start_hour": 9}]
```

`五234` = 星期五第 2~4 節 = 09:00–12:00,對得起來就沒問題。

## 6. 產生向量 ← **吃 GPU 的一步**

```sh
uv run python -m prep.embed
# [embed] 載入 BAAI/bge-base-zh-v1.5(device=mps) ...
# [embed] 維度 768 與契約相符。
# [embed] 3472/3472 (100%)
# [embed] 建立 HNSW cosine 索引 ...
```

裝置自動挑 `cuda` → `mps` → `cpu`,也可以自己指定:

```sh
EMBED_DEVICE=cuda uv run python -m prep.embed
```

只處理 `embedding IS NULL` 的列,中斷後直接重跑就會接續。3472 門課在 Apple Silicon
(MPS)上約數分鐘,純 CPU 明顯久很多。

這個 repo 刻意**不**把 torch 釘在 CPU wheel index(理由見 `pyproject.toml`),所以
Linux/Windows + NVIDIA 會直接拿到 CUDA 版,macOS 原生 wheel 本來就支援 MPS。

## 7. 產出成品

出貨前會擋兩件事:`schema_meta` 與契約不符、還有課沒有向量。

```sh
uv run python -m prep.dump
# [dump] 檢查通過:3472 門課,全部有向量。
# [dump] 完成:out/course-1142.sql.gz(74 MB)
```

`pg_dump` 是在容器裡跑的(一般開發機不會裝 postgres client),用 `docker compose ps` 找
自己專案的容器 —— 不會誤抓到查詢端那個。可用 `PG_CONTAINER` 覆寫。

格式是 `pg_dump -Fp --no-owner --no-privileges | gzip`,不是 `-Fc`:postgres 的 entrypoint
只認得 `*.sql` / `*.sql.gz` 這類,`.dump` 會被 ignore 掉。用 `.sql.gz` 的話查詢端把檔案丟進
`db/init/` 就會自動還原,不需要任何自訂腳本。兩種格式實測同樣 74MB、資料逐位元相同。

## 8. 交給查詢端

```sh
cp out/course-1142.sql.gz ../CourseLangChain/db/init/
cd ../CourseLangChain
docker compose down -v         # ← 一定要 -v
docker compose up -d postgres
```

查詢端的 `db/init/` 只放**一個**成品檔;原始資料留在這個 repo,不要複製過去。

> [!IMPORTANT]
> restore **只在資料庫第一次建立時**發生(容器 initdb 機制)。換成品沒有 `down -v`
> 清掉 volume 的話,新檔案不會生效,而且不會有任何錯誤訊息。

---

## contract.yaml

`contract.yaml` 在這裡與 CourseLangChain 的**設定值必須一致**(註解可以各寫各的 ——
比對的是解析後的值)。`prep.migrate` 會把這些值寫進資料庫的 `schema_meta` 表,
查詢端開機時比對,不一致就拒絕啟動。

擋的是這個失敗模式:這邊換了 embedding 模型重新灌資料,那邊沒跟上 —— query 向量與庫裡的
向量來自不同模型,**檢索不會報錯,只會安靜地回一堆語意無關的課**。

確認兩邊有沒有對齊:

```sh
uv run python -c "import yaml;a=yaml.safe_load(open('contract.yaml'));\
b=yaml.safe_load(open('../CourseLangChain/contract.yaml'));print('aligned:',a==b)"
```

改契約的時候:兩個 repo 一起改 → 重跑 `migrate` → 清掉舊向量 → 重跑 `embed` → 重新產 dump。

## 各步驟在做什麼

| 模組 | 作用 |
|---|---|
| `prep.migrate` | 套 `init-scripts/02~04`,建 `schema_meta`。其中 `03-schema.sql` 建 BM25 索引 `USING bm25 (pk, (content_text::pdb.jieba))` —— `pk` 是代理鍵,因為 ParadeDB 的 `key_field` 必須唯一,而來源的 `course.id` 有重複(101883 列只有 81226 個不同 id)。 |
| `prep.populate_time` | `time_raw` → `sessions` jsonb / `weekdays` / `has_morning` 等旗標。解析器 `course_time.py` 是從查詢端 `utils/time.py` 抄過來的硬化版(兩個 repo 刻意不共用程式碼)。 |
| `prep.embed` | 用 `contract.yaml` 指定的模型產向量寫進 `course.embedding`,最後建 HNSW cosine 索引。 |
| `prep.dump` | 出貨前擋契約不符與缺向量,然後在容器裡跑 `pg_dump -Fp … | gzip`。 |
| `prep.build_sqlite` | **【已棄用】** 舊 SQLite/FAISS 路徑的索引。需要 `uv sync --extra sqlite`。 |

## 疑難排解

### embedding 模型下載失敗(401 / GatedRepoError)

`contract.yaml` 指到的是 gated model(要在 HuggingFace 申請授權)。要嘛設好 `HF_TOKEN`,
要嘛換成非 gated 的同維度模型 —— 兩個 repo 的 `contract.yaml` 一起改,再重跑 `prep.embed`。

### 建 HNSW 索引時 `could not resize shared memory segment`

Docker 預設只給 `/dev/shm` 64MB。這個 repo 的 compose 已經設 `shm_size: 1gb`;
若是自己另外起的容器記得加上。

### `prep.dump` 說「還有 N 門課沒有向量」

`prep.embed` 沒跑完。直接重跑,它只會處理還沒做的列。

### 維度不符

`[embed] 模型維度 X 與 contract.yaml 的 embed_dim=Y 不符`。換模型要同時改 `embed_dim`,
而且 `course.embedding` 欄位型別已經是 `vector(Y)` —— 要重跑 `migrate` 並清掉舊向量。

## 已棄用:SQLite / FAISS

查詢端的 `USE_SQLITE` 已標記棄用 —— PostgreSQL 現在做的是同一件事(BM25 + 向量 + RRF),
而且只要一份成品,不用 `data.db` + `faiss_index/` + `courses.jsonl` 三份成品。
`prep.build_sqlite` 只為了讓還沒搬完的環境能繼續運作,不要拿它做新東西。
