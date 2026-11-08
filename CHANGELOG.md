# 变更记录

本项目遵循语义化版本：主版本 = 输出格式变化（订阅端 / 设备要跟着改）；次版本 = 新增能力或排版规则调整；
修订号 = 只修 bug，输出应逐字节不变。

每次改动都在这里记一条。**改排版必须重跑 `tools/golden_snapshot.py --write` 更新行为基线**，
并把「改了什么、为什么」写在这份文件里——否则下次没人知道基线是被谁动的。

## [1.21.11] - 2026-11-08

**扛高并发：差异化边缘缓存 + 源站 gzip 压缩（serve.py 仅改响应头/传输，feed 输出逐字节不变）**

- **问题**：此前 `serve.py` 对所有响应一刀切 `Cache-Control: no-cache`，配合 Cloudflare
  对 `/rss` 的 **Bypass cache** 规则，导致**每一个访客、reader.html 每 5 分钟轮询的每一次**
  都穿透到 Pi 源站。feed.xml 有 ~286KB，高并发下既是请求量放大器、也是 Pi→Cloudflare
  隧道的带宽放大器；限流阈值（全局 2400/min）在真有流量时反而会变成「误伤正常访客」的开关。
- **差异化缓存**：新增 `_cache_policy_for()`，按「文件多久变一次」给缓存意图——
  `feed.xml` / `feed-wrap.xml` / `rotate.json` / `ai-accum.json`（每 5 分钟重建）给
  `public, max-age=120, s-maxage=120, stale-while-revalidate=600`；`ai-digest.html` /
  `sitemap.xml` 给更长 TTL；页面给 `max-age=60`；**看板 PNG 保持 `no-cache`**（必须保住
  Kindle 的 304 省流量行为，不能让它因边缘缓存而每次下整张图）。
- **配套改 Cloudflare 边缘**：`TUNNEL.md` 第 5 步把 `/rss` 的 Cache Rule 从 **Bypass cache**
  改成 **Cache everything + Edge Cache TTL 120s**（Cloudflare 默认不缓存 `.xml`/`.html`，
  不显式开规则、源站头再对也没用）；`/kindle` 那条**保持 Bypass**。效果：访客几乎全被边缘
  吃掉，源站只见缓存未命中/重校验，请求量下降一两个数量级；feed 5 分钟才变、边缘只缓存 2 分钟，
  最坏晚 2 分钟，阅读器无感。
- **源站 gzip**：客户端（经 Cloudflare 透传）带 `Accept-Encoding: gzip` 时，对文本类响应
  （xml/json/html/css/js/svg/txt）按需压缩（≥1KB 才压、压了更大就不压），带
  `Content-Encoding` + `Vary: Accept-Encoding`；HEAD 永不发压缩长度，缓存仍存未压缩字节。
  把 Pi→Cloudflare 隧道那段 286KB 的 feed 压到 ~40KB，缩减上行带宽。
- **连接队列** `request_queue_size` 64 → 128：给缓存刚过期那一瞬的并发穿透与极端突发留余量。
- **限流保持**：边缘缓存后回源量骤降，2400/min 的全局阈值退化为纯防滥用，不再误伤正常流量；
  不再调高（避免放松滥用防护）。
- 仅改 `Pi/serve.py` 的响应头与传输层，feed.xml 字节、设备版、页脚内容均未变；golden 基线不受影响。
  64 例 serve 测试全绿（新增 gzip / 差异化缓存 / PNG 保持 no-cache 等用例）。

## [1.21.10] - 2026-11-07

**AI 重点页：送 AI 前按语义近似去重 + 解析截断容错**

- 送 AI 之前新增 `_dedup_similar`：对「最新在前」的送选前缀按标题字符相似度
  （difflib ratio，默认阈值 0.72）折叠同事件的冗余稿，只留每个事件簇里最新的
  那条。62 个源常对同一事件用不同措辞发稿，此前这些近似稿混在 196 条候选里既
  挤占 token 又让 AI 在重复事件里挑；折叠后 AI 看到的是去重后的事件集合、挑出的
  20 条更多样。注意这不是「排除已推荐条目」——用户定全天不去重，这里只折叠
  同一时刻同一事件的多份冗余副本，不影响隔几小时因重要性变化再次入选。
- `parse_response` 容错：新增 `_salvage_json_array`（用 `json.JSONDecoder.raw_decode`
  从头逐个解析完整对象，遇到第一个截断处停下），让免费 8B 偶发的 max_tokens 截断
  （最后一个 {...} 不完整、无收尾 ]）只丢最后那条残稿、其余完整稿照常出，页面少
  一条远胜整页停更。`_extract_json_array` 常规解析失败（典型截断）时改走抢救而非
  抛错；`parse_response` 也直接容错原始字符串，双保险。实测 14/16/17/20 点那类
  「JSON 无法解析 → 整页走兜底」由此消除。
- `main()` 抽出成功路径 `_generate_and_write` 压住函数行数（≤60）；日志新增
  「去重后 N 条」便于核对折叠效果。无输出格式变化，golden 基线不受影响。
- 443 例测试全绿（test_ai_digest 61 例），review_check / gate_contracts 双门禁通过。

## [1.21.9] - 2026-11-07

**修正 ai-digest.timer 节拍回归（3x/day → 每小时）**

- `Pi/services/ai-digest.timer` 的 `OnCalendar` 被改回 `*-*-* 07..21:00:00`（白天 07:00–21:00
  每小时触发、夜间不跑），与「当日滚动池」设计一致；此前不知何时回退成 `07,13,19` 一天三次，
  与 1.21.5 起的口径相悖。Description 与注释同步更新。
- 该单元一直在 git 内（`services/` 子目录，由 `deploy.sh` 拷贝安装），并非脱离版本管理；
  本次只是把偏离的节拍校正回文档既定意图。`Persistent=false` 保持不变（重启不补跑、不重复耗配额）。
- 仅改部署节拍，无代码/输出变化，无需重跑 golden 基线。

## [1.21.8] - 2026-11-06

**AI 重点页：当日滚动池 + token 预算送选（存储与送选分离）**

- **存储与送选分离**：滚动池存储上限 `AI_ACCUM_CAP` 240 → **400**（tmpfs 廉价，尽量保住
  全天条目）；真正送 AI 的条数不再等于存储上限，改由 **token 预算**决定——新增
  `_budget_items`：从滚动池按「最新在前」（`_merge_accum` 已保证）截取前缀，每条候选估
  150 token（标题+来源+摘要≤120 字 ≈170 字，Llama-3 分词留余量）、固定开销 500 token
  （system+尾部格式约束），预算 `AI_PROMPT_TOKEN_BUDGET`（默认 30000）约可送 **196 条**，
  超出真机验证过的 120 条包线，若 CF 端上下文不足则兜底保留上一期页面（安全失败）；
  预算再小也至少送 1 条，绝不交白卷。
- **成本核算（写进注释）**：单次输入 ≈196 条 ×150 ≈ 29.9k token ≈ 123 neurons（4119/1M），
  输出 ≈1.2k token ≈ 42 neurons（34868/1M），合计 ≈165 neurons/次；白天 15 次触发
  （指纹未变还会跳过）≤2.5k neurons/天 ≈ 25% 免费档 10,000。
- **一致性铁律**：`parse_response`、签名/跳过闸门（`_pool_signature`）、`render_html` 的
  pool_size 全部改用**送选前缀**——AI 实际看到几条，页面「从当日 N 条 RSS 候选中筛选」
  就写几条；指纹也算在前缀上，前缀外的旧条目被挤出存储上限不再触发重算白烧额度。
  `_log` 同时打印滚动池条数与送选条数。
- **顺带重构**：`main()` 里两套后端分派抽成 `_invoke_llm`（main 已超 60 行提醒线）。
- 用例 426 → 431（`tests/test_ai_digest.py` 44 → 49：`_budget_items` 预算内全收 /
  超预算只留最新前缀 / 预算极小至少 1 条，以及 main 以 550 预算验证 prompt 只含 1 条
  候选、页面候选数同步为 1）。

## [1.21.7] - 2026-11-06

**AI 重点页：精华概况强制 ≤10 字（渲染端硬截断兜底）**

- 实测 v1.21.6 上线后，20 条精华里有 7 条超 10 字（如「油价下跌不意味市场危机结束」13 字）——小模型未严格守
  ≤10 约束。新增**渲染端硬截断**：`render_html` 把每条 `reason` 取前 10 个字符（`r["reason"][:10]`），
  保证页面 100% 视觉规整，与「宁丢不配错」的兜底哲学一致（模型不守规则则代码强制）。
- 提示词同步收紧：SYSTEM 与两套尾部模板都改为「严格 10 个汉字以内…超出部分会被系统直接截断丢弃」，
  硬性规则 ④ 注明「务必控制在 10 字内」，降低触发截断的概率。内部字段名与解析逻辑不变。
- 新增测试 `test_render_html_truncates_essence_to_10_chars` 锁定该兜底。

## [1.21.6] - 2026-11-06

**AI 重点页：每条卡片理由压成 ≤10 字精华概况（利于一页快速扫完）**

- 把每条卡片那行「为何值得关注（≤25 字理由）」改为 **≤10 个汉字的精华概况**，整页 20 条更一眼扫完。
  内部字段名仍为 `reason`（测试与解析逻辑不变），只改了提示词语义与占位文案（「（未提供理由）」
  →「（未提供精华概况）」）。`title↔reason` 配对防线（理由等于任何标题即判无效置空）保持不变。
- 提示词同步收紧：SYSTEM 与两套尾部模板（对象/数组）都明确「用不超过 10 个汉字概括这条新闻的
  精华要点，不得照抄标题」，硬性规则 ④ 改为「reason（≤10 字精华）只能概括 title 那条」。
- 模型对比实测（同候选池、温度 0.1、max_tokens 8192）：
  - `@cf/meta/llama-3.1-8b-instruct-fp8-fast`（现用默认）：3/3 达标，去重后 31–33 条、几乎零重复；
  - `@cf/qwen/qwen3-30b-a3b-fp8`（同价 MoE）：精华质量同级，但**重复率高**（返回 24–25 去重后仅
    15–21），去重后常不足 20，**暂不作为默认**。两模型均可通过 `LLM_MODEL` 环境变量切换。

## [1.21.5] - 2026-11-06

**AI 重点页：修文案 + 省额度（源未变跳过 CF + 定时器白天每小时、夜间不跑）**

- **修文案**：页面描述与副标题原写「由 AI 从当日 20 条 RSS 候选中筛选」，把「输出 20 条」和
  「候选池 120 条」搞混了。现改为报**候选池大小**（`render_html` 新增 `pool_size` 参数，
  由 `main` 传 `len(items)`，即喂给模型的 120 条池），文案变为「从当日 120 条 RSS 候选中筛选」。
- **省免费额度（源未变跳过 CF）**：新增候选池指纹（`_pool_signature`，对 120 条池的
  标题/链接/时间做 sha256）。`main` 在调用模型前比对指纹——与上一期一致且已有产物页则**直接
  跳过 CF 调用、保留现有页面**；指纹变了（哪怕只增删/换位一条）才重算。成功生成后写回新指纹。
  这样静态时段（如深夜、新闻少的清晨）不白烧配额。
- **定时器改为白天每小时、夜间不跑**：`ai-digest.timer` 由「一天三次 07/13/19」改为
  `OnCalendar=*-*-* 07..21:00:00`（07:00–21:00 每小时触发，22:00–06:00 不跑）。配合上面的
  跳过逻辑，实际 CF 调用只在「池子真的变了」时发生，15 次/天的触发里多数会跳过。
- 新增 5 个测试：候选池文案、指纹稳定性、以及 `main` 在「源未变→跳过 / 源变了→重算」两种路径。

## [1.21.4] - 2026-11-06

**AI 重点页可靠性：把「凑满 20 条」从「碰运气」做成「稳态」**

1.21.3 部署后真机复跑发现：页面要么 14 条、要么 17 条，远不到 20。逐条归因（在 Pi 上
跑诊断脚本复用 `_pick_item` 作真值，多轮采样）揪出两个真凶，都不是匹配 bug：

1. **模型重复选同一条**：某期返回 24 条里 8 条是重复，去重后只剩 16。免费 8B 模型在
   `temperature=0.3` 下爱「列清单」式地重复。→ 把温度压到 **`LLM_TEMPERATURE=0.1`**
   （挑选题是确定性任务，低温更照章办事），并在提示词加硬性规则「每条只选一次、不得重复、
   恰好 n 条、title 须逐字含【栏目】标记」。压到 0.1 后重复近乎归零（多轮 28–60 返回里
   0–3 条重复）。
2. **模型无视条数、超额返回 + 输出撞 token 上限被截断**：它根本不理「挑 24 条」，常返回
   28–60 条；1.21.3 的 `max_tokens=6144` 在返回 40+ 时被顶满、JSON 截断 → 整页走兜底。
   → `max_tokens` 抬到 **8192**（免费档实测可稳装 60 条；再高会触发网关超时，故止步），
   客户端超时从 30/45s 放宽到 **60s**（实测 8192 token 生成落在 60s 内，旧 45s 是误伤）。

附带：reason 上限 40→25 字，压低单条开销、给 token 预算让路；`_ASK_BUFFER` 因模型无视
条数已无意义，回收到 4（仅作「万一未来模型开始听话」的余量）。

真机多轮采样（temp 0.1 / 8192 / 60s）：**5/5 轮去重后 ≥28、截留稳定 20 条**，无截断、
无超时；失败（偶发网络/限流）仍走「保留上一期 20 条好页」兜底，页面永不为空、永不少 20。

## [1.21.3] - 2026-11-06

**AI 重点页：把 `max_tokens` 从 4096 抬到 6144，稳装「多问 4 条」的缓冲请求、杜绝 JSON 截断兜底**

1.21.2 上线后真机复跑抓到一类会让整页静默走兜底的情况：1.21.0/1.21.1 已把每次向模型
请求的条数从「正好 20」抬到「20 + `_ASK_BUFFER`(4) = 24 条」，而 `call_openai` 的
`max_tokens` 仍停在 4096。免费 8B 模型返回 24 条带理由时偶发把 token 顶满，JSON 被截断
（缺收尾的 `]`）——`_extract_json_array` 抛 `ValueError` → 整页走「保留上一期好页」的
兜底，于是某个节拍出来的是 18 条甚至更旧的一期，而日志里只报「对上 M 条」、没有失败信号。

修法：把两处输出上限对齐到「稳装 24 条」的实测值——`call_openai` 默认
`max_tokens` 与 Gemini `maxOutputTokens` 都由 4096 / 2048 抬到 **6144**。实测 n=24
配合 6144 返回 29 命中 27、JSON 完整闭合；试过 8192 会触发 Cloudflare 30s 网关超时，
故取 6144 为上限而非更高。`_keep_top`「只截断、不重新配对」的语义不变，缓冲条数照旧
留作安全余量，只是这次从根上不再因截断而丢整页。

纯运行参数调整，输出格式与 1.21.2 逐字节一致，无订阅端/设备依赖。

## [1.21.2] - 2026-11-06

**AI 重点页：多问几条做缓冲，稳定凑满「一页 20 条」**

用户要求「挑前 20 条，一页刚好看完」。1.21.0/1.21.1 上线后真机观察：免费 8B 模型
偶发把回显标题改写到对不上池子原文，被「宁丢不配错」的安全机制丢弃，某期只凑出 18 条。

修法：让 `main()` 向模型**多问 `_ASK_BUFFER`(4) 条**，模型返回后由新增的 `_keep_top()`
按返回顺序**只保留前 `AI_TOP_N`(20) 条**——既给安全丢弃留余量、能稳定补足 20 条，
又绝不因凑数而容忍张冠李戴（`_keep_top` 只截断、不做任何重新配对）。日志同步加「请求 N 条」。

用例 419 → 420（`tests/test_ai_digest.py` 新增 `_keep_top` 顺序截断断言）。

## [1.21.1] - 2026-11-06

**AI 重点页三项修正：标题回显配对加固（简繁归一 + 相似度兜底）、时间统一新加坡时间**

1.21.0 上线后真机复跑发现两个残留问题：模型回显标题时把繁体字转成简体
（线上实拍「銀河娛樂」→「银河娛樂」，一字之差精确与包含都配不上，一整期
只对上 8/20 条），以及页面上各源时间混着显示原文时区（GMT / +0800 / 无
时区），读者没法横向比较新旧。

- **简繁归一（用户明确要求「简体/繁体要正确处理」）**：`_norm_title` 在去
  空白后追加繁→简归一（约 340 对常用字的对照表，方向唯一、基本一对一），
  **只用于比对**——回显↔池子、理由防线、去重全部归一后进行；页面显示的
  标题永远是池子源原文，繁体源就显示繁体，绝不因这张表改写输出。表本身
  有格式自检用例（每个词「繁简」两字、繁体键不重复——maketrans 遇重复键
  会静默取后者）。
- **相似度兜底**：归一后仍配不上的「模型轻微改写」（漏字/换说法），与全池
  逐条算 difflib 相似度，≥0.75 才算同一条。宁可丢一条，也绝不把理由配到
  错的新闻上。
- **时间统一新加坡时间（用户要求）**：卡片时间由源 pubDate 原文改为
  `_fmt_date_sgt` 统一转 UTC+8 显示（「11-06 12:21 SGT」）；无时区按项目
  惯例当 +8，完全不像日期就原样显示，不因时间脏而丢条。
- **可观测性**：生成日志加「返回 N 条 / 对上 M 条」，配对率下降一眼可见。
- **页面标题改名**：`PAGE_TITLE` 从误带设备名（「Dot Quote / 0」，那是墨水屏设备的名字，
  不属于这页）的「AI 每日重点」改为语义清晰的「RSS 新闻 · AI 的目光」；同步更新
  `og:description`。纯展示文案变更，输出格式不变，无订阅端/设备依赖。

用例 410 → 419（`tests/test_ai_digest.py` 21 → 37：简繁归一、对照表格式
自检、简繁整条互转命中、轻微改写相似度救回、SGT 时区转换/无时区/脏日期、
title 优先于 idx、理由抄他人/自己标题判无效、同条去重等）。



**AI 每日重点页正式上线：Cloudflare Workers AI 免费档（无需绑卡）**

之前这一页一直卡在「后端不可用」：Gemini 被 Google 应用层地区封锁（HTTP 400），
DeepSeek key 零余额（402）。这次换成 **Cloudflare Workers AI 免费档** —— 10,000 Neurons/天、
无需绑卡、OpenAI 兼容端点，实测每天三次、每次从 120 条里挑 25 条带理由，只消耗约 1,800
Neurons（不到免费额度的两成）。Token 只写在树莓派的 `/home/user/site/llm.env`（600，
不进仓库、不回显），脚本缺 key / 超时 / 限流 / 脏数据一律保留上一期或写占位页，退出码 0，
绝不连累 rss-build / moreaix-site。

**修了两个会让这页静默走兜底的真问题**：

- **默认模型已下线**：`@cf/meta/llama-3.1-8b-instruct` 已于 2026-05-30 下线（Cloudflare 返回
  410 Gone），换用同免费档、更快的 `@cf/meta/llama-3.1-8b-instruct-fp8-fast`。
- **json_object 强制对象、模型自选键名会解析失败**：OpenAI 兼容端点走 `response_format=json_object`
  时顶层必须是对象，模型实际会自选键名（如 `{"fruits": [...]}`），而旧的 `_extract_json_array`
  只认 `items` 键 —— 解析失败 → 走兜底 → 永远出不来真页。现在 OpenAI 后端提示词显式要求
  `{"items": [...]}` 对象、Gemini 后端保持裸数组（匹配各自的格式约束），`_extract_json_array`
  也兜底取「第一个值为列表的键」，两种后端都稳出真页。`max_tokens` 提到 4096 防截断。

实测（2026-11-06 12:57 Pi 真机）：Cloudflare 返回 25 条带理由，写出
`/dev/shm/rss-hub/ai-digest.html`，经 `--alias /rss` 在 `moreaix.com/rss/ai-digest.html` 返回
200，页面自 canonical、零站内交叉链接、只外链原文。sitemap 同步加入该页。

用例 400 → 403（`tests/test_ai_digest.py` 18 → 21：补模型自选键名兜底、提示词按后端区分对象/数组）。

## [1.21.0] - 2026-11-06

**AI 重点页修复「标题与下面那行小字不匹配」；挑条数 25 → 20（一页刚好读完）**

线上实拍（2026-11-06 13:00 那期）：免费 8B 模型对着上百条编号挑 25 条时，`idx` 与
`reason` 常常配错位 —— reason 干脆抄了**下一条新闻的标题**，页面变成「标题 A 配
小字 B」（用户原话：标题与下面那行小字不匹配；例：标题「光轮智能与宇树科技达成
战略合作」下面挂的是「AI失去的第一个秋天」）。根因不在映射代码（`idx→title` 映射
是对的），在模型输出本身张冠李戴，代码却照单全收。

**修法是让配对由模型自己锁死，代码不再信任编号**：

- 提示词改为让模型对每条选中项**回显它选中的标题**（`{"title": "逐字照抄", "reason": "..."}`，
  title 与 reason 放进同一个 JSON 对象），Gemini 的 `responseSchema` 同步改成
  title/reason 两个 STRING；
- `parse_response` 以**标题对标题**映射（去空白精确匹配 → 包含式容错 → 旧式 idx 兜底），
  同一条重复回显只保留第一次；
- **理由防线**：reason 去空白后若恰好等于池子里任何一条的标题（含本条，即模型又在
  抄标题冒充理由），判无效置空，渲染显示「（未提供理由）」—— 页面永远不会出现
  「那行小字是另一条新闻的标题」。

挑条数 25 → 20（`AI_TOP_N` 默认值 + service `Environment=` 同步；用户要求 20 条
刚好一页快速看完）。`AI_TOP_N` 的代码默认值改回字面量写法，重新进参数表契约
（包常量会静默退出门禁核对，见 review_check `_env_calls` 的说明）；OPERATIONS.md
参数表补上该行。

用例 403 → 410（`tests/test_ai_digest.py` 21 → 28：标题回显映射、空格漂移容错、
title 优先于 idx、理由抄他人/自己标题判无效、同条去重、非字典与对不上即跳过）。

## [1.20.0] - 2026-11-06



## [1.19.0] - 2026-11-06

**每日日志快照改为落盘持久化：每天只写一次盘，重启后仍可查**

用户明确要求「每天落盘一次」。1.18.0 那份每天 00:05 的快照原本在
`/dev/shm/journal-archive`（tmpfs，重启即清），这次把它定向放开到 SD 卡上的
`/var/log/journal-archive`：

- `Pi/journal_snapshot.py` 的 `SNAPSHOT_DIR` 默认值（仍保留 `os.environ.get`
  写法，参数表靠它提取）、`journal-snapshot.service` 的
  `Environment=JOURNAL_SNAPSHOT_DIR=`、`OPERATIONS.md` 参数表三处一起改到
  `/var/log/journal-archive`。新增跨文件契约检查
  `check_journal_snapshot_dir`：两处必须相等，且都不得是 `/dev/shm` / `/run`
  这类 tmpfs 路径；解析不出来（文件缺失、写法换了、Environment= 被删）一律报必改。
- 目录与权限交给 systemd 的 `LogsDirectory=journal-archive`：`/var/log` 是
  root:root 755，`User=user` 自己建不了目录，由 systemd 建好并赋权，不需要
  `deploy.sh` 手工 mkdir/chown，也不需要 sudo 脚本。它默认建 0755、旧 tmpfs 是
  0700，gz 里又是完整系统日志，所以单元补 `LogsDirectoryMode=0700` +
  `UMask=0077`：目录 0700、写出的 `.tmp` / `.gz` 0600，仅属主可读；脚本写文件
  逻辑一字未动（两条指令在 systemd 252 的 systemd.exec 里都支持）。
  `ProtectHome=read-only` 保留（写入已不在 `/home` 下）；没有启用
  `ProtectSystem=strict`，这套组合没在 Pi 上实测过，单元注释写明了依据与取舍。
- timer 仍是 `OnCalendar=*-*-* 00:05:00` / `AccuracySec=1min`、仍不写
  `Persistent=true`。单元注释与运维文档改成真实理由：重启后 volatile 日志为空，
  补跑会导出一份空内容并按当天日期覆盖掉已有的好快照，所以错过这一拍就等下一天，
  不再拿「不往 SD 卡写 stamp 文件」当理由（本来就要落盘了）。
- 清理逻辑一字未动：仍按文件名里的日期只删 `journal-YYYY-MM-DD.log.gz` 形状、
  只留最近 3 份、不认识的文件不碰、`.tmp` + `os.replace` 原子写。
- **一次性切换代价**：改目录不搬旧数据。Pi 上 1.18.0 留在
  `/dev/shm/journal-archive/` 的当天快照仍在 tmpfs、重启即清，脚本不做迁移；
  部署后手动触发一次 `journal-snapshot.service` 即可把当天这份补成落盘快照。
  OPERATIONS.md「日志存储」已写明，`deploy.sh` 不需要加迁移步骤。
- 脚本头注释、运维手册参数表 / 每日快照段 / 代价段 / 写盘实测段、测试模块说明里的
  旧说法逐处改掉。`Pi/serve.py`、`build_feed.py`、`render.py`、`rotate.py` 与
  feed 内容一律未动；journald 仍是 `Storage=volatile`（`/run/log/journal`，内存）。

**口径（2026-11-06 01:50 Pi 实测）**：实时日志仍在内存、重启即清；每天 00:05 只有
一次 gz 快照落到 SD 卡，体积随当天日志量波动 —— 「约 10KB」是 1.18.0 的样本、不是
固定值，本次手动触发实测 20,449 B / 1317 行纯文本、`zcat` 可读，目录 700、文件
600，`df -T` 确认在 `/dev/mmcblk0p2` ext4 持久分区。快照能跨重启但**只留 3 天**。
常态（不触发快照）60 秒窗口整卡写入 155,648 B ≈ 2.53 KB/s，部署前同口径基线
176,128 B ≈ 2.87 KB/s，同量级；手动触发一次的前后 3 秒窗口整卡写入
126,976 B ≈ 124 KB（含首次建目录、服务启动记录、`jbd2` 元数据提交），快照文件
本身 20 KB —— 那是一天一次的那一拍，不是常态速率。两者同量级，说明除每天
00:05 那一次外没有新增持续写盘。

用例 372 → 382（`tests/test_journal_snapshot.py` 12 → 16 例、
`tests/test_gate.py` 56 → 62 例）。H11 反例：`.qa-tmp/red_check_snapshot.py`
新增四条契约反例（单条检查与整轮门禁一起验红）、`.qa-tmp/red_check.py` 新增
一条，这两处**新增条目**均实跑确认变红；入库的反例用例是 `tests/test_gate.py`
里 6 个 Sandbox 用例。

**已知遗留（与本次无关）**：`.qa-tmp/red_check.py` 里改动前既有的「pubDate 极端值」
条目退回缺陷后仍绿，使该脚本整体退出码为 1；QA 已在 `git archive HEAD` 干净副本上
复核，HEAD 同样如此。本次未改动 `render.py` / `tests/test_hardening.py`，这个既有
H11 缺口不在本版范围内。

**上线验收与文档校正（2026-11-06 02:10，不升版本）**：在 Pi 上跨一个完整的
5 分钟窗口复验设备版滚动 —— `feed-wrap.xml` 的 `guid` 从 02:05 的
`f55804349a49` 换成 02:10 的 `53aae45e2197`，`lastBuildDate` 与公网
`Last-Modified` 同步跳档，`rotate.json` 的 `window` 加 1、`shown` 从 9 条增到
10 条、`current` 与新 `guid` 一致；六个服务单元都是 `Result=success`、
`NRestarts=0`，`systemctl --failed` 为 0，近 1 小时无错误日志。完整记录见
OPERATIONS.md「上线验收记录」。顺带校正三处与实际脱节的说明：README 的订阅源
61 改成 62（含单轮耗时、抓取算式和「49 × 5 = 245 条」的旧写法）、首页仍写着
「实际约 1~3 小时一版」的旧说明（GitHub 早已改成每天 3 次）、以及 `rss.yml` 的
push 白名单漏掉 `docs/*.html`（改静态页不触发构建，Pages 会一直停在旧文案）。
仓库补上首个版本标签 `v1.19.0`。以上只改文档与 workflow 触发条件，feed 内容、
设备版输出、PNG 与所有运行产物逐字节未变。

## [1.18.0] - 2026-11-06

**日志按天留存到内存盘：一天一份快照、只留 3 份、3 天以上自动清除**

承接产物内存化与 11-06 的 journald volatile 改造：日志已不写 SD 卡，代价是
`/run` 是 tmpfs、重启即全清，而 journald 自己轮换出的 `.journal` 是二进制文件，
既不能直接读也拿不走。这次补上「每天一份可读的快照」：

- 新增 `Pi/journal_snapshot.py` 与 `journal-snapshot.service` / `.timer`：每天
  00:05 把过去 24 小时日志（`journalctl --since -24h -o short-iso`）导成 gzip
  纯文本，写到 `/dev/shm/journal-archive/journal-YYYY-MM-DD.log.gz`，`zcat`
  可读、可 scp。物理上仍在 tmpfs，一天约 10KB，**SD 卡写入不增加**。
- 清理按**文件名里的日期**而不是 mtime：只留今天与前 2 天共 3 份，第 4 份起
  自动删；不符合 `journal-YYYY-MM-DD.log.gz` 形状的文件一律不碰。写 gz 用
  `.tmp` + `os.replace` 原子替换，写失败时清掉 `.tmp` 且不破坏上一份。
- journald 的整机配置同步收紧：`MaxRetentionSec=7d → 3d`，并显式写
  `MaxFileSec=1day`（原来靠默认值），让「一天一个文件」和「3 天自动清除」
  在配置里看得见。配置在 Pi `/etc/systemd/journald.conf.d/10-volatile.conf`，
  **不进仓库**，改法与代价见 OPERATIONS.md。
- `Pi/deploy.sh` 的上传清单、单元清单、enable 清单与状态自检一起加新单元；
  门禁参数表补 `JOURNAL_SNAPSHOT_DIR` / `JOURNAL_SNAPSHOT_KEEP_DAYS`。

刻意没给 timer 加 `Persistent=true`：它会在 `/var/lib/systemd/timers/` 落一个
stamp 文件（又写回 SD 卡），且重启后 volatile 日志本来就是空的，补跑没有内容。
**代价不变**：重启或断电，内存日志与这 3 份快照一起清空；要跨重启只能外发或
落盘，两者都与当前目标冲突，没有擅自做。

用例 360 → 372（新增 `tests/test_journal_snapshot.py` 12 例）。

## [1.17.0] - 2026-11-05

**安全审计：补齐服务端缺口；边缘层缺口移交给 Cloudflare 控制台**

上机用裸 socket 打了 40 多组畸形请求。核心路径（目录穿越、编码绕过、扩展名
白名单、隐藏文件、请求走私、敏感配置）全部挡住，但抓到四处服务端缺口：

- **非 GET/HEAD 完全绕过闸门**：基类对未定义的 `do_*` 直接回 501，不经
  `_precheck()`。实测「伪造 Host 的 POST」照样 501 —— Host 白名单、URL 校验、
  限流一起被跳过，501 还把请求方法名原样回显。现在 `__getattr__` 兜住**任意**
  方法，统一先过闸再回 405 + `Allow: GET, HEAD`，不再回显方法名。
- **限流表可被撑大**：原写法先 `setdefault` 再判全局额度，被 429 拒绝的伪造
  IP 也会建条目；清理又只删「空 deque」，一次性灌进来的条目永远变不空。
  改成全局额度先判、判满不建条目，每 256 次放行扫一遍过期条目。
- **重复 Host 只查第一个**：第二个伪造值能白嫖白名单（实测 200）。改为
  `get_all()` 全量校验；URL 控制字符补上 DEL(0x7f)。
- **200/304 仍逐条写日志**：只聚合 4xx 时，正常 GET 一样能指挥 journald
  写 SD 卡。现在所有状态码按分钟汇总一行，代价是丢掉逐条 IP/路径明细。
- beacon 配置也当外部输入：token 改显式 ASCII 白名单（`isalnum()` 会放行全角
  字母与阿拉伯数字）；`CF_BEACON_JS` 必须 https 且不含引号/尖括号。
- systemd 沙箱补 `ProtectSystem=full`、`PrivateDevices`、
  `CapabilityBoundingSet=`（空）、`RestrictAddressFamilies` 等 12 项。
  `ProtectSystem=strict` 仍不能用（会把 `/dev/shm` 一并设只读）。
- 宽松 CSP 只放 `frame-ancestors/object-src/base-uri`，不设 `default-src`：
  首页内联 canvas 脚本与 CF 统计不会被掐掉。

**边缘层仍缺（需控制台操作）**：实测公网响应没有 HSTS、没有完整 CSP，也没有
针对大文件的速率限制；WAF 托管规则与 Bot Fight 是否开启未知。这些只能在
Cloudflare 侧配，详见 OPERATIONS.md「内存盘产物」一节。

行为变更两处：非 GET/HEAD 从 501 改 405；访问日志从逐条改成按分钟汇总。
用例 350 → 360，新增 11 条反例逐个退回全部变红（旧 11 条同步回归有效）。

## [1.16.0] - 2026-11-05

**三件事：产物全搬进内存盘、`serve.py` 收敛攻击面、请求不再反复读盘**

### 1. 产物写入全部离开 SD 卡

原来白天每 5 分钟一轮会写 400KB 的 `feed.xml`，而 `kindle-screen.path` 盯的正是
它的 mtime —— 内容没变也会连锁重画两张 PNG（约 200KB）和 40KB 网页。实测公网
`GET /analytics.env` 还直接返回 200（49 字节配置文件），暴露面比想象中大。

- **内容没变就不写**：`build_feed._write_if_changed()` 比较前摘掉
  `<lastBuildDate>`（这个字段每轮都刷成现在，直接比全文永远不相等）。比较用的
  原文由 `load_prev_items()` 顺手带回 —— 那 400KB 本来就要读一次做失败回退，
  不必为了比一次再读一遍。这一处让整条重绘链一起停下。
- **产物搬进 `/dev/shm/rss-hub`**（tmpfs，Pi 上可用 453MB，全部产物不到 1MB）：
  `rss-build` / `rss-rotate` / `kindle-screen` / `moreaix-site` 四个单元与
  `Pi/rotate_screen.py` 的路径一起改。SD 卡上只留 `feeds.txt` 配置副本与站点
  静态页源文件，都是低频读、几乎不写。
- **「零写盘」只对本项目的产物成立，不等于整机 SD 卡零写入**。上机按
  `/proc/diskstats` 的扇区口径实测：静置 60 秒块层仍写 1.23MB，不同窗口
  3.6～34 KB/s 波动（journald 周期性刷盘造成的突发，不是匀速）。进程级归因
  `systemd-journald` 占八成以上，其余是 `tailscaled` 与 ext4 的 `jbd2` 提交；
  旧产物 mtime 冻结在改造那一刻、`feeds.txt` 冻结在 11-01，确认 RSS 侧一笔没写。
  连这部分也要清掉得改系统级配置（journald `Storage=volatile`，重启即丢日志），
  不属于本项目范围。
- 目录由各单元的 `ExecStartPre=/usr/bin/install -d -m 700` 幂等创建；两个静态页
  在 `moreaix-site` 启动时复制进内存盘；`ExecStartPost` 异步触发一轮抓取，把
  重启后的空窗从「最多 5 分钟」缩到「一次构建」。
- **已知取舍**：重启后轮播游标重置一次（内存盘不持久），站点在首轮构建完成前
  这些动态产物是 404；Kindle 端会保留上一张图。换「单进程纯内存」能免掉空窗，
  但要合并四个服务、丢掉原子替换与进程解耦，不值。

### 2. `serve.py` 收敛攻击面

- **扩展名白名单 + 隐藏文件拒绝**：`analytics.env`、`*.tmp/*.new` 半成品、
  `.git`、未知扩展名一律 404。以前是「文件存在就发，不认识的类型给
  octet-stream」，等于把站点根目录整个挂上公网。
- Host 白名单（挡 DNS rebinding）、URL 长度与 NUL/控制字符校验（含 NUL 的
  路径原来会让 `realpath` 抛 ValueError，整条连接 500）、内存滑动窗口限流
  （429 + `Retry-After`，按 `CF-Connecting-IP` 分访客 + 全局兜底）、连接读超时
  8 秒（Slowloris）、`daemon_threads`、四个低风险安全响应头。
- **错误日志按分钟汇总**：4xx 逐条写 journal 等于扫描器能指挥 SD 卡写盘。
  实测发现 `send_error()` 是先 `log_error()` 再 `send_response()`，那时状态码
  还没落到 `_code`，只在 `log_message` 里判断会让聚合被整个绕过 —— 补了
  `log_error` 覆盖，日志输出复核后逐条 404 归零。

### 3. 请求路径只做一次 stat

- 响应字节按 `(dev, ino, size, mtime_ns, ctime_ns)` 缓存在内存里（含注入后的
  HTML），命中连 `open()` 都不做；`os.replace` 换 inode 或原地改写都会让 key
  失配并重读，不会把新内容压成旧内容。原来每个请求 `isfile` + `stat` +
  `open/read` 三次调用，400KB 的 feed 每次都重读一遍。
- 缓存与限流状态挂在 `make_handler` 造出的子类上（每个实例一份），不落盘、
  测试互不污染；上限 32 项 / 2MB。304 直接由 stat 回答，连缓存都不查。

行为变更两处，都写进了用例：未知扩展名从「照发 octet-stream」改成 404；
4xx 访问日志从逐条改成按分钟一条汇总。用例 331 → 344。

**还没清零的两处**：每轮构建仍从 SD 卡读一次 `feeds.txt`（内容相同就跳过写，
根分区挂 `noatime`，这次读不会产生元数据写）；200/304 成功请求仍逐条写访问
日志，只聚合了 4xx —— 正常 GET 一样能指挥 journal 写盘，流量上来会是新的
放大器。

## [1.15.0] - 2026-11-05

**新增：Cloudflare Web Analytics 的手动 beacon 接入（只在 moreaix.com）**

Cloudflare 控制台的自动注入对 moreaix.com 不生效：这个 hostname 只走 Tunnel，
不是账号下的 Website（zone），控制台明确要求自己装 JS Snippet。实测打开自动注入后
等 150 秒、两种 UA、三个页面，响应里始终没有 beacon —— 而同一份 HTML 里
Cloudflare 的邮箱混淆脚本一直在注入，说明边缘 HTML 改写本身是通的。

- `Pi/serve.py`：给 HTML 响应在 `</body>` 前注入 beacon。只改 HTML、只插一次、
  token 为空就完全不注入；注入在算 Content-Length 之前完成，GET 与 HEAD 长度一致。
  一处覆盖 `/`、`/rss/`、`/kindle/` 三页。`docs/index.html` 与 GitHub Pages 是同一份
  源文件，但改写只发生在 HTTP 响应里，磁盘文件和 Pages 上的页面都不受影响。
- `Pi/services/moreaix-site.service`：加 `EnvironmentFile=-/home/user/site/analytics.env`。
  token 放 Pi 上、不进仓库；文件不存在（`-`）时服务照常启动且不注入，于是
  「先发版、后给 token」两件事解耦，给 token 后只需重启服务。
- token 有字符白名单：混进引号或尖括号就拒绝注入，不让一个配置写错弄坏整页 HTML。
- 用例 317 → 331（`test_pi_serve.py` 20 → 34）；6 条反例把每条防线逐个退回，
  对应用例全部变红。顺手修掉测试文件里一处脏串。

## [1.14.0] - 2026-11-01

**全量代码审核后的加固：修的是「不报错、只是结果悄悄变错」那一类**

过了一遍全部代码（36 个文件），下面这些共同点是：出错时不抛异常、日志里也算成功，
只有产物是错的 —— 所以每条都补了能把它弄红的用例，并用 `.qa-tmp/red_check.py`
把修复逐个退回、确认对应用例真的会变红（不是摆设）。

**会写坏产物的三条**

- `feeds_sync.py`：清单超过 2 MB 上限时被**静默截断成半份**并落盘。`read(n)` 是
  「最多读 n 字节」不是「超过 n 报错」，半份照样过条数校验，而且下一轮本地已等于
  这份截断体、判定为 `same`，再也回不到完整清单。改成多读 1 字节来分辨「刚好等于」
  与「超过」，超了就整份拒掉。
- `build_feed.py` 的编码判定：源声明 `ISO-8859-1` / `cp1252` 时中文整源变乱码且算
  「抓取成功」。这类单字节编码能解码**任意**字节、永不抛错，排进候选就等于关掉
  utf-8 / gb18030 整条兜底链。现在把它们剔出可信声明。
- `board.sh`：临时图固定成一个文件名，而常驻循环与「立即刷新一次」是**并发**的
  （扩展的设计就是循环正在跑也能点），两个 wget 交错写同一个文件出来的必然是半张图；
  循环那边的 `rm -f` 还会删掉 `--once` 刚下完的图。改成按 pid 命名。

**会崩在 import 期的一条（改一处就全站停摆）**

`ROTATE_SECONDS=""` 这类空串会让 `int("")` 抛 ValueError，而这些取值全在模块级 ——
异常发生在 import 期，`main()` 进不去、`--version` 也打不出来。数值型参数统一走
`_int_env` / `_num_env` 回落默认值。
连带修了一处**门禁自身**的问题：它靠 `os.environ.get("NAME", "默认值")` 这一种写法
提取参数表与节拍契约，换了写法之后那些参数会从契约里**静默消失**（不报红，只是从此
不再核对）—— 现在两种写法都认，并补了一条盯住这件事的用例。

**会抛异常、且抛在写盘之前的两条**

`0001-01-01T00:00:00Z`（.NET 模板把 `default(DateTime)` 序列化出来的经典值）的 epoch
是 -6.2e10，`datetime.fromtimestamp` 在负区间上抛 `OSError` —— 而调用它的两处都在
写盘之前，一抛就是「feed 不更新、状态不落盘」。解析层钳到 0（与「解析失败」同口径），
两处调用点各自再兜一层；坏时间最多让这条不带 `<pubDate>`，不赔上整份产物。

**准确性**

- `arrival.settle` 的记忆链补了一层**源级**兜底：原先只有条目级记忆，而条目会被
  `ITEM_LIMIT` 裁出产物，一裁出去它的「首见时间」就消失了 —— 下次带着源刷新的新时钟
  回来时查不到记录，于是把新时钟当成首见时间永久锁定，**条目真的变年轻**，「怎么老是
  这条」隔几小时复发一次就是这么来的（用 ITEM_LIMIT=3 复现过）。
  失真源整批共用一个时间戳，所以「上一轮该源任何一条的时间」即可兜底。
  代价写进了注释：该源换新稿时新稿也会拿到上一轮那个时间、显得偏老。
- 正则兜底通路 `<link href='…'>`（单引号）取不到链接 → 阅读器里这条**打不开原文**；
  DOCTYPE 内部子集里带 `]` 时会剥一半、残留 `]>` 把整源打进丢字段的兜底通路；
  未知实体名的长度上限从 2~9 放宽（原上限让「未知实体换成空格」那句承诺落空）。
- 「成功 / 回退 / 失败」三个数原来加起来会超过源总数（失败里含回退），
  现在打的是「失败且没得回退」，那才是真正少了内容的源。

**设备端其余**

`board.sh --once` 成败都 `exit 0`，失败时仍会去画上一张旧图（画面「动了一下」像是
刷新成功了），而诊断文件里 `rc` 永远是 0 —— 改成失败退出 1；`refresh.sh` 把 board.sh
的 stdout 接进 `rssboard-diag.txt`（否则 wget 报错原文丢失，排障主路径断了）；
锁文件只验 `kill -0` 会在 pid 复用时永远判定「已在运行」（屏上一动不动而日志说在跑），
加 `/proc/<pid>/cmdline` 归属校验；`is_png` 补验结尾的 IEND 块（超时留下的半张图文件头
完好，原来会放行并让屏幕从此不再变化）；`rotate_screen.py` 的临时文件改 `mkstemp`
（固定 `.tmp` 既会互相踩，又是一个公网能直接取到的半成品）；`build-mrpackage.sh` 的
换行自检在 `od` 缺失时会静默放行，改成跑不起来就失败；`Pi/deploy.sh` 补建
`/home/user/site` 并上传主页静态文件（换机部署时缺这一步会让唯一的公网出口反复重启）。

用例 306 → 317（新增 `tests/test_hardening.py`）。

## [1.13.0] - 2026-11-01

**新：看板实时图页 `docs/board.html`，扫码就能在手机上盯那块屏**

需求是「扫码访问 `https://moreaix.com/kindle/rss-pw2.png`，并且每 5 分钟自己刷新一次」。
二维码不能直接编那个 PNG 地址：那是张静态图，打开一次就定住了，不会自己动。
所以新加一个页面把图嵌进去，由页面负责按节拍取新图。

页面里两个必须这么写、否则自动刷新根本不生效的点：

- **每次取图都要换 URL**。源站响应头是 `Cache-Control: max-age=14400`，同一个地址浏览器
  四小时内压根不会重新请求；加一个每次不同的 `t=` 参数，既换掉浏览器缓存键，
  实测 Cloudflare 也回源（`cf-cache-status: MISS`），拿到的确实是新画面。
- **先在后台 `new Image()` 把图下完，成功再替换 `img.src`**。直接改 `src` 会先清空旧图，
  网速慢时用户看到的是一块白；先下的话换图是瞬时的，画面不闪。

另外 iOS 锁屏或切后台会**冻结定时器**，所以回到前台时若已过原定时间点就立刻补刷一次；
连续取图失败 3 次会停下来等人点按钮，不会无限空转。刷新节拍 5 分钟，与设备上
`rss-rotate.timer` 的节拍对齐。首页页脚加入口。

## [1.12.0] - 2026-11-01

**新：一个「扫了就能订阅」的手机页 `docs/subscribe.html`**

想在 iPhone 上订阅这份 feed，扫码比手抄地址靠谱得多。但 iPhone **没有内置 RSS 阅读器**，
所以二维码不能直接指向 `feed.xml`——那样 Safari 只会把 XML 原文甩一脸，一步都用不了。

新的 `docs/subscribe.html` 就是二维码的目标页，移动端优先、深浅色跟系统：

- 主按钮走 `feed://` 协议：系统会把链接连同订阅地址交给 iPhone 上已装的阅读器
  （NetNewsWire / Reeder / Follow 这类认 `feed://` 的），一步到位。
- 备用路径：复制 https 地址 + 三步粘贴说明 + App Store 搜索入口**（没装阅读器的人也有下一步可走）——
  因为 `feed://` 没人接管时 iOS 会弹「打不开该网页」，光给一条必然要有兜底。
- 首页页脚加了入口；页面本身不渲染 feed 内容，不带版本号，也就不需要跟着版本走。

不动 `tests/golden.txt`：这次完全没碰渲染器，输出逐字节未变。

## [1.11.1] - 2026-11-01

**修：Kindle 看板一张图都没出过屏——是 https 证书换了新链**

现象是「KUAL 里点刷新毫无反应、屏上永远停在旧画面」。设备侧取证：根目录既没有
`board.png` 也没有 `rssboard-diag.txt`，而这两样只要成功过一次就一定存在；
`board.lock` 里残留的 pid 说明常驻循环今天 07:57 起来过，每轮 fetch 全败。

真因在服务端：`moreaix.com` 的证书两天前（10-30）换到了 Let's Encrypt 的新链，
末端是 **ISRG Root X2**，KPW2 那套 2019 年的根证书里没有它 —— Kindle 的 wget
证书校验**秒失败**，而 wget 的错误原文连看都看不到，光盯屏幕永远分不清是 TLS 还是没网。
同地址的 http 直连 200、不跳转。

改动：

- `kindle/board.sh`：`fetch()` 改成 https 先试（超时 8 秒）→ 失败自动退到同一地址的
  http（25 秒）；抽象出一个 `http_url()` 用 sed 换协议，避免「两个地址变量各自漂移」。
  **这条是为了让下一次证书轮换不再让看板哑掉**，而不是修一次就算。
- `kindle/extension/rssboard/bin/doctor.sh`：自检把 https 与 http **分开验**，
  `fetch` 行现在会直接给结论：`OK-https` / `FAIL-https OK-http` / `FAIL-both`；
  屏上那行 verdict 相应变成 `WARN: TLS not trusted, http works`。TLS 与网络从此能当场分开。
- `kindle/README.md`：排障表里「手动把 URL 改成 http」那条已过期，改成自动退让的说明。

不动 `tests/golden.txt`：这次改的是 Kindle 设备端的 shell，渲染器输出逐字节未变。

排查过程中还记一笔工具教训：**`grep -c $'\r'` 在这台 Windows 上的 Git Bash 里恒等于行数**
（等于用空模式匹配了每一行），一度把不是 CRLF 的文件全报成 CRLF，白查一轮。
查行尾要用 `od -An -tx1 f | tr ' ' '\n' | grep -c '^0d$'` 或 python 数 `\r\n`。

## [1.11.0] - 2026-11-01

**新：Kindle 看板搬出 `Pi/`，成为与自己平级的独立子项目 `kindle/`**

以前 Kindle 那一摊藏在 `Pi/kindle/` 里，而它里面的东西**一件都不跑在树莓派上**
（`board.sh` 与 KUAL 扩展跑在 Kindle 设备上），另外两个渲染器虽然跑在树莓派上，却要
靠 `Pi/deploy.sh` 顺手带上——改 Kindle 一个参数就得跑一遍 Pi 的全套部署。两者的划分
标准也因此混乱。

现在按「谁的设备谁的代码」重新归位，**两个目录各自具备完整的生命周期**：

| | 之前 | 现在 |
|---|---|---|
| 设备端客户端 + KUAL 扩展 | `Pi/kindle/board.sh`、`Pi/kindle/extension/` | `kindle/board.sh`、`kindle/extension/` |
| 两个渲染器 | `Pi/kindle_screen.py`、`Pi/kindle_page.py` | `kindle/kindle_screen.py`、`kindle/kindle_page.py` |
| 看板的两个 systemd 单元 | `Pi/services/kindle-screen.{path,service}` | `kindle/services/` 下同名两个 |
| 部署入口 | 混在 `Pi/deploy.sh` 一条循环里 | **`kindle/deploy.sh`**（新的，与 `Pi/deploy.sh` 平级、互不引用） |
| 文档 | `Pi/KINDLE.md`、`Pi/JAILBREAK.md` | `kindle/README.md`、`kindle/JAILBREAK.md` |

三条边界，写在这里以免以后又糊在一起：

1. **两个 `deploy.sh` 互不引用、互不安装对方的单元**。`Pi/deploy.sh` 现在只管静态服务与
   轮播；Kindle 那边断掉也不会拖累设备版。
2. **树莓派上的路径一个都没变**（仍是 `/home/user/pi-pipeline/` 与 `/home/user/kindle/`），
   线上不用迁移、不用停机，只是由哪个脚本传过去的差别。
3. **仍然共用同一台机器与同一个公网出口**：`Pi/` 的 `serve.py` 继续负责把 `--alias /kindle`
   发出去。这条是刻意的——它是运行时的合作，不是代码上的依赖。

配套改掉的：
- CI 的语法检查加上新目录；门禁的「模块要有测试」把 `kindle/*.py` 列为必改（它有两个渲染器
  的 47 例配套用例，比 `Pi/*.py` 那种「只能靠部署验证」的状态齐全得多）
- 两个 Kindle 测试从 `test_pi_kindle_*.py` 改名 `test_kindle_*.py`
- 根 README 的架构图与目录树补上 `kindle/` 这条分支，并写明两个项目平级
- `OPERATIONS.md` / `Pi/README.md` / `Pi/TUNNEL.md` 里所有指向旧路径的描述一并更正；
  `Pi/README.md` 的边界表从「两套系统」扩成「三套」，并写明两者只是恰好同一台机器

### 顺带做掉的代码审核（找到了三个真缺陷）

搬之前把 12 个文件逐个读了一遍，顺手修掉的问题按严重度排：

1. **`build-mrpackage.sh` 少了 LF 转换**（最严重）。`build-extension-zip.sh` 打包时会把
   CRLF 转成 LF，但这条路径**没有** —— 而它恰恰是最容易踩的那条：重装整机才用一次，
   平时根本没人碰，一旦工作区在 Windows 上变回 CRLF，产出的 `.bin` 装上去就是
   「脚本一行都不执行、屏上毫无提示」，正是本项目最想防的那种失效。现已补上
   全量转换 + 打完自查，并实测过正反两面：喂一个 CRLF 文件它会拦下（数到 3 个 CR），
   转换后放行（0 处）。
2. **`board.sh` 的清屏工具写死成了 `eips -c`**，而画图用的是 `$DRAW`。把 DRAW 改成
   `fbink -g` 之后，清屏仍然走 eips —— 两者没报错、只是不生效：fbink 有自己的显存
   快照，它记着的还是 eips 画的内容，于是清屏清到别的帧、残影越积越多。现已抽出
   `$CLEAR`，注释里写明两个变量必须是同一个工具。
3. **`common.sh` 里的 `$LOG` 是死变量**：定义了 `$EXT/log.txt`，但没有一个脚本写它；
   而 `board.sh` 的注释却写着「输出重定向到扩展目录的 log.txt」。实际所有人都往
   `$DIAG`（根目录的 `rssboard-diag.txt`）写。删掉死变量、把注释改成事实。

另外修了 `uninstall.sh` 里一笔抄死的绝对路径（改用 `$EXT`，与 `restore.sh` 一致），
以及 `common.sh` 首行混进去的一个多余英文词。

**RSS 输出不受影响**（引擎代码没动），304 例全绿，门禁 0/0/0。

## [1.10.0] - 2026-11-01

**修：时间戳失真的源会永远排在最前（「为什么老是三星那条」）**

现象是完整版 feed 的头部永远是同一批、同一条。查下去是源的行为所致：
日经中文网的 RSS 给**整份 feed 打同一个 `<pubDate>`**，且每轮重新生成就刷成当前时刻 ——
于是它们每一轮都自动成为「全场最新」，稳占前五个位置；而并列时的次级键固定，
同一批稿子里永远是同一条排第一。

先量了 62 个源：**只有 2 个是这样**（日经中文网、光明日报·版面），
其余 60 个的 `pubDate` 逐条各不相同，是真发布时刻（德国之声 63 条里 62 个不同时间、
纽约时报跨度 82 小时）。所以**源的发布时间默认就是可靠时间，仍然照用** ——
只对自动检测出的失真源做修正。

判据一条：**同一源这一批条目的时间戳完全相同** → 那不是发布时间，是这份 feed 的生成时钟。
此时改用「我们第一次见到这条的时间」。这个时间取自**上一轮的产物**（那里每条带着我们
当时认定的时间），所以不需要新增状态文件，重启也不用预热；取 min(上一轮, 本轮标称)，
保证年龄只增不减。

改写必须同时动两个字段：`ts`（排序依据）和 `date_raw`（写进产物的字符串）——
只改前者会被后者覆盖回去，下一轮读回来又是源标称的时钟，记忆链断掉，等于白改。
tests/test_arrival.py 有专门一条盯这个往返。

新增 `arrival.py`（12 例）。补用例的过程里查出一个真 bug：`prev_seen_index` 的
`ts < out.get(key, ts)` 在键不存在时拿自己当基准，`ts < ts` 恒假，索引整张是空的。

顺带补了 `tools/audit_source_dates.py`（14 例）：逐个源体检「发布时间能不能信」，
按严重程度报 无时间 / 未来时间 / 共用时间戳 / 逐条改时间 / 正常，两次采样隔一段时间
才看得出「同一条的时间会不会变」。源的行为会随时间改变，这件事只能靠定期体检发现，
不能指望一次修完；有了这个脚本，下次再有人问「为什么老是某某那条」时能立刻给出证据。

## [1.9.0] - 2026-11-01

**新增：源清单不再靠人工 scp，Pi 每轮构建前自己去仓库取**

起因是当天实测的一个事实：仓库那份 `feeds.txt` 改过之后没有推到 Pi，两边一度
相差 3 行注释（11020 vs 10523 字节）。这事的坏处不是报错，而是**没有任何信号** ——
两边各自出一版 feed，条目构成不同，而屏幕与 Kindle 都只会显示内容，不会显示「清单版本」。

现在的做法：新增 `feeds_sync.py`，`main()` 在读清单之前调用一次；三个新参数

| 参数 | 默认 | 用途 |
|---|---|---|
| `FEEDS_SYNC_URL` | 空（不同步） | Pi 上配仓库 raw 地址；CI 不配，那边清单本来就是 checkout 出来的 |
| `FEEDS_SYNC_TIMEOUT` | `20` | 拉清单超时秒数 |
| `FEEDS_SYNC_MIN_SOURCES` | `10` | 至少几个地址行才算清单（挡 404 页 / 跳转页 / WAF 拦截页） |

`Pi/services/rss-build.service` 里已配 `FEEDS_SYNC_URL`，所以**改完 `feeds.txt` 推上去，
最多等一个节拍（白天 5 分钟、夜间 1 小时）就在 Pi 生效**，不用再手工 scp。

一条原则贯穿这个功能：**同步失败绝不能让整份 feed 停摆**。拉不到、超时、返回非 200、
内容不是 UTF-8、像 HTML 页、地址条数不够、甚至写不进磁盘，一律退回本地那份继续构建。
理由是同步失败在屏上的表现与「全网故障」一模一样，却没有留下任何可查的线索——
宁可这一轮用旧清单，也比让 feed 停更、且没人知道为什么好。
`tests/test_feeds_sync.py` 的 12 例就是这条底线的逐条固化。

为什么单独一个模块：它是件自成一体的小事（取回 → 判断像不像清单 → 原子替换），
不碰条目、不碰 XML、不碰轮播；塞进已经最重的 `build_feed.py` 会让那个文件继续变长
（提醒线 750 行），而它与抓取唯一的耦合是「必须在读清单之前跑」。

旧的 Pi 侧 `feeds.txt` 副本从此只作为「拉不到时的兜底」存在，**不再是唯一依据**。

## [1.8.0] - 2026-10-31

把上一轮审计里唯一剩下的 Info 清零：`Pi/` 三个脚本此前一个测试都没有。**补的过程中查出
一个真的排版缺陷**（见下第 1 条），所以它按次版本发，不是补丁。

**修：`wrap()` 可能返回 `max_lines + 1` 行**

1. `Pi/kindle_screen.py` 的折行循环里，两处 `lines.append()` 都是**追加之后**才数行数，
   放不下时最多可能多出一行。`max_lines` 对调用方是排版契约 —— `_draw_list()` 按
   `len(lines)` 预留每条的高度，多给一行会把页脚或下一条挤出可用区。现在循环结束后硬截断
   到 `max_lines`，随后的省略号分支因为「末行不再是原文结尾」会自动补上「…」。
   反例：`test_max_lines_is_respected`（把修复撤掉就红：`AssertionError: 3 != 2`）。

**补：`Pi/` 三个脚本的用例（+64，178 → 242）**

2. `tests/test_pi_serve.py`（15 例）：**真起一个 127.0.0.1 的服务**用 urllib 打进去，而不是给
   Handler 打桩——被测的三条规则都在 HTTP 头层面。覆盖：目录穿越（`../` 四种写法 + 未经解码的
   `%2e%2e%2f` 一律 404，且外部内容绝不出现在任何响应里）、root 内的符号链接穿不出去、
   `/rss` 前缀剥与不剥指向同一文件、304 / 过期条件头 / 坏条件头当没带、**404 也必须带 CORS**、
   `Server` 头不含 Python 版本、未知扩展名给 octet-stream、HEAD 只有 Content-Length。
3. `tests/test_pi_rotate_screen.py`（21 例）：锁「按需落盘」这个每天省 288 次 SD 卡写入的行为
   ——只变了 `<lastBuildDate>` 不算内容变化（不摘这个字段的话该优化一次都不会命中）；
   rotate.json 坏 JSON / 存了列表 / 文件缺失都要退化成空状态而不是崩；
   设备版地址由完整版 `<link>` 推导（`feed.xml` → 同目录 `feed-wrap.xml`）；
   跑真 `main()`：产出恰好一条的设备版、同一窗重复跑不推进、池子空时保持上一版并退 1。
4. `tests/test_pi_kindle_screen.py`（28 例）：把那次「整版双字叠印」永久钉住——
   用 spy 断住 `_draw_list` 被调用**恰好一次**且传的是完整列表（`27f28f1` 删 `_draw_lead`
   时漏删那行的后果）；折行用自己写的定宽度量器而非真实字体（每行几十毫秒且与字体无关）；
   超宽原子必须硬切、行数硬上限、孤字行退成一行加省略号；PNG 是 `.tmp + os.replace`
   且魔数完整；池子空时不覆盖旧图。

**配套改动**

5. CI 的 review.yml 装 `pillow==11.3.0`：`kindle_screen` 依赖它，本机没装时那批用例会跳过，
   CI 必须装上才验得到。版本号固定以免上游变动。
6. 用例里的示例地址一律用不含点的占位主机名（`https://FEED_HOST/rss/feed.xml`）：
   门禁的「硬编码外链」规则认的是带域名的地址，测试夹具不该为了绕过它而削弱规则。
7. README 目录树与用例数同步：**242 例**（core 57 / title_layout 11 / rotate_edge 39 /
   gate 56 / state_hygiene 10 / dedupe 4 / golden 1 / pi_serve 15 / pi_rotate_screen 21 /
   pi_kindle_screen 28）。

至此**门禁首次全绿：0 阻断 / 0 必改 / 0 提示**（此前只剩 Pi 三条 Info）。

## [1.7.4] - 2026-10-31

上一版审计的收尾。没有改动运行时行为（`golden.txt` 逐字节不变），动的全是「验证能力」本身：
自己的检查会说谎、自己的文件装不下。

**修：门禁把格式问题说成「文件不存在」**

1. `check_test_count_sync` 从 README 目录树抽用例数时，只要行尾不是 `# N 例` 这个格式
   （比如写成 `# core 逻辑 68 例`），就会落到「目录树里没有 test_x.py」这条必改上 ——
   文件明明在树里，文案却在骗人，照它去改会加出一堆重复记录。改成显式的五态判定：
   一致 / 行尾无计数 / 树里没提到 / 文件不存在 / 数字不符，每态一条自己的文案。
2. 顺手加一道同类守卫：**同一文件在目录树里写了两处计数也报必改**。留两行等于没有唯一答案，
   和 `ROTATE_MAX_LAG`、`sim --seed` 是一类病 —— 参数在，但没人能证明它起作用。
3. 五态各有反例，外加一条「四态文案互不相同」的总闸门：只断言「有没有输出」是假绿，
   必须证明文案会随状态变。

**重构：四项跨文件契约检查搬进 `tools/gate_contracts.py`**

4. `check_version_sync` / `check_workflow_sync` / `check_rotate_timer_sync` / `check_test_count_sync`
   连同专属 helper 搬出 `review_check.py`：892 → **599 行**（项目自设的提醒线是 750，
   上一行长到 892 是因为一夜之间加了太多检查）。
5. `code_defaults` / `as_number` / `_doc_table_values` / `_doc_lines` **留在原处**：grep 过调用方，
   它们还被 `check_doc_sync` 和 `_check_hardcoded_urls` 用着，搬走就要让旧模块反向依赖新模块。
   引用方向只有一条：`gate_contracts` → `review_check`，反向靠 `main()` 里的延迟 import；
   import 失败会留一条必改，不让这四项静默消失。所有跨模块访问写成 `RC.xxx` 而不是
   `from review_check import xxx` —— 绑成局部常量会让把 `RC.ROOT` 指向临时目录的用例全部假绿。
6. 迁移是等价的：拆分前后跑门禁，报告逐行比对唯一差异是那条文件行数提示消失；
   `main()` 里 26 项检查的调用顺序、扫描文件数 24 → 25（新模块进来了）——都没动。

**测试与文档**

7. `tests/test_core.py` 763 → 694 行：`TestWidth`(2) + `TestSplitTitle`(9) 迁到新的
   `tests/test_title_layout.py`。总数 166 → 178（新增 12 条全在门禁自检上）。
8. `OPERATIONS.md` §刷新节奏补一句：**改轮播节拍只能写 `rss-rotate.service` 的 `Environment=`**。
   实测 systemd 报 `Unknown key 'Environment' in section [Timer], ignoring` —— 写在 `.timer` 上
   不会传给被它拉起的服务，届时节拍还是 300 而 timer 每 10 分钟才推进一次，症状和上一条漂移一模一样且无报错。

**真机验证（审计最后一环，在 Pi 上跑的）**

9. `kindle-screen.path` 换成 `PathChanged` 后确实会触发：先用 rename 模拟 `_write()` 的 `os.replace`
   （inode 133639 → 133192，2 秒内拉起），再跑一次真实构建闭环，`rss-build.service` 结束的同秒
   `kindle-screen.service` 启动并重画 120 条。**不需要回退 `PathModified`，也不用加 15 分钟兜底 timer。**
10. `Pi/serve.py` 的条件请求生效：`If-Modified-Since` 返回 **304**；`Server:` 头不再泄漏 Python 版本；
    设备版三个 URL 全部 `cf-cache-status: DYNAMIC` + `access-control-allow-origin: *`（预览页跨域读取用得上）。
11. `Pi/rotate_screen.py` 切到 `render.device_description()` 后连续 5 窗正常推进，
    「内容未变跳过写入」也确实生效（SD 卡写入从每天 288 次降到有变化才写）。

## [1.7.3] - 2026-10-31

用户要求「审核一次所有代码」。做了**三路独立审查**（引擎核心 / Pi 部署层 / 工具链与测试），
报告分级：**2 个阻断、13 项必改、20 余项可选**，另有 **16 处「注释说的和代码做的不是一回事」**。
这一版把阻断与必改全部修掉、可选里便宜的一并做掉，并把注释与文档按代码事实重写一遍。
没有新增任何环境变量。

**阻断与安全**

1. **看板整版双字叠印（线上可见）**：`Pi/kindle_screen.py` 连着两行 `_draw_list()`，第二行传
   `items[1:]`、两次都从同一个 `y` 起笔 —— 除最后一行外每行都是两行字叠印，整版不可读。
   来源是 `27f28f1` 删 `_draw_lead()` 时漏删下面那行（前一次重构 `3660c4d` 是干净的，
   它「md5 不变」的说法成立）。已删，并在原处留注释说明「统一规格后不存在头条区+列表区两段」。
2. **兜底解析通路没过消毒**：`_sanitize()` 原本只在 ElementTree 通路调用；源里带控制字符
   （如裸 `U+0001`）时 ET 必然解析失败 → 走正则兜底 → 控制字符原样写进 `feed.xml` →
   **整份完整版对所有订阅端非法**（`ET.fromstring` 报 invalid token），且只要该源还在就一直坏。
   改成在 `parse_items()` 入口统一消毒（幂等，两条通路共用）。
3. **解压上限是假的**：`_cap(gzip.decompress(body))` 是**先全量物化再检查**，压缩比可到 1:1000
   （实测 20 MB 全零压成 20 KB），12 MB 响应能炸出 GB 级、runner 直接 OOM。改用
   `zlib.decompressobj(...).decompress(body, MAX+1)`，`unconsumed_tail` 非空即判越界。
4. **显式剥 `<!DOCTYPE>`**：实体炸弹此前只是被 `&…;` 白名单「偶然」挡住（11 字符的实体名照样展开，
   实测 805 字节 → 10 万字符）。加一道显式防护，成本为零。

**正确性**

5. **状态数值字段不裸 `int()`**（`rotate._int`）：`{"window":"abc"}` / `[1]` / `{}` 会抛
   `ValueError`/`TypeError`，而 `rotate_pick()` 两个调用方都没有 try —— Pi 上表现为
   **每 5 分钟崩一次、`rotate.json` 永远不被重写、屏幕永久停在上一版**，无自愈。
   现在：能 `int()` 就接受（数字字符串是合法值，不能当坏值丢掉），`window`/`cycle` 坏值当缺失，
   `dwell` 坏值按「节拍不符 → 丢整份状态重开」。补 4 条用例（含 `window=str(w)` 与 `int` 等价、
   `dwell=600` 必须重开），并把 `test_state_hygiene` 模块头「只覆盖列表字段」的窄口径改正。
6. **`build_rss()` 缺 `src` 键会 KeyError**：`esc(it["src"])` → `esc(it.get("src") or "")`，
   与它自己 docstring 承诺的「缺键不该让整份 feed 生成失败」对齐。
7. **标题尾巴剥掉了数字**：`split_title()` 剥「标题 - 来源名」的正则原本吃 1~8 个任意字符，
   于是 `苹果发布会-2024` → `苹果发布会`、`财报速递 | 2024` → `财报速递`。年份/编号是标题信息，
   尾巴字符类改为不含数字。**这条会改变个别条目的标题行**，属于排版修正，由 golden 基线逐行锁住。
8. **原子写的临时名固定**：两个进程同时写同一路径时，A 的 `os.replace` 会让 B 的写入落进
   被替换的旧 inode，原子性失效。改用 `tempfile.mkstemp(dir=同目录)` + `chmod 0644`
   （mkstemp 默认 0600，直接替换会让别的用户跑的 web server 读不到）。

**Pi 无人值守（这一层以前完全没有机器检查）**

9. `rss-build.service` 加 `TimeoutStartSec=900`：默认 90 秒，而 49 源 × 15s 超时 × 重试 / 并发 10
   最坏要 155 秒 —— 网络稍差那一轮会被 systemd SIGTERM 掉，feed 停在旧版且没有任何告警。
10. `rss-serve.service` 的 `User=pi` / `/home/pi/...` 改成 `user` / `/home/user/...`（它是唯一一份
    `pi` 口径的单元，照它重装会起不来 → Tunnel 502）。`kindle-serve.service` 加
    `ExecStartPre=mkdir -p`（首启目录不存在会 `sys.exit`，之后也不自愈）。
11. `kindle-screen.path` 的 `PathModified=` → `PathChanged=`：`build_feed._write()` 用
    `os.replace` 换掉了 inode，`IN_MODIFY` 不会发生，systemd 只会记 "possibly broken watch" 并重挂。
12. `rss-rotate.service` 显式写 `ROTATE_SECONDS=300` / `ROTATE_POOL_MAX=120`（原本靠代码默认值，
    改默认值会静默改掉 Pi 的行为），并注明必须与 `rss-build.service` 一致。
13. `serve.py`：`realpath` 代替 `abspath`（root 内符号链接可指向 root 外）、`sys_version = ""`
    （不再暴露 `Python/3.13.x`）、加 `Last-Modified` + `If-Modified-Since` → 304
    （Kindle 每 5 分钟拉 ~80 KB 图，一天 45 MB）。**没有动 `Cache-Control` 与 CORS 头**。
14. `board.sh`：下载后校验 PNG magic（`serve.py` 的 404 会带 HTML body，`-O` 落盘再 `mv`
    会把 `board.png` 写坏）、加 `flock` 单实例保护。

**门禁：把它自己的七个「静默通过」补上**

15. `H8` 以前只查环境变量**名字**在不在文档里 —— 把参数表里的 `90` 改成 `60` 照样全绿。现在
    解析表格取值与代码默认值比对（这正是本仓历史上「参数表写 60、代码早已是 90」那次事故的形状）。
16. 生成物**缺失**以前 `continue`（删掉 `minute.yml` 或 `golden.txt` → 「全部通过」）；生成器
    **抛异常**以前降级成 Info（超时也「通过」）；`subprocess.run` 不看 `returncode`。三处都改成必改。
17. 凭据扫描没传 `cwd=ROOT`：从 `docs/` 跑门禁时 `git ls-files` 只列子树，根目录里的 key 扫不到。
18. 版本号是子串匹配（`v1.7.20` 能顶替 `v1.7.2`）、`H4` 是子串匹配（往测试里加一句注释就算「有测试」）、
    `docs/` 被整体排除（真正发布的两个页面从不参与 HTML 检查）—— 依次改成边界匹配、AST 收集 import、
    对 `docs/*.html` 单独跑安全子集。
19. **新增 `check_rotate_timer_sync`**：`ROTATE_SECONDS` 与 `Pi/services/rss-rotate.timer` 的
    `OnCalendar=*:0/5` 必须表示同一节拍。这是本项目第三个「假旋钮」（前两个：`ROTATE_MAX_LAG`、
    `sim_rotate --seed`），症状是改了参数、屏幕节奏没变（跳条 / 同一条连显）。
20. **新增 `tests/test_gate.py`（44 例）**：门禁过去零测试，上面这些「静默通过」全是它的后果。
    每条检查都要有一个「能把它弄红」的反例（`CODE_REVIEW.md` 新增 **H11**）。

**注释与文档**

21. **16 处与代码不符的注释全部按事实改**（`CODE_REVIEW.md` 新增 **H10**：注释写的是承诺，
    改行为必须同提交改注释）。典型：`SCREEN_OUT` 注释说「压缩前后各查一次」而第二道在解压之后；
    `rotate.py` 说「最近 3 窗 = 15 分钟」，真实最短间隔是 4 窗 = 20 分钟；`render.py` 说设备版
    单条体积「降到三成」，实测 88%。
22. **补齐 docstring**：引擎 11 个函数（`_write`/`main`/`dedupe`/`text_width`/`esc` 等）、
    Pi 层 20 处（`serve.py` 原本 8 个函数一个都没有）、`tools/` 19 个、测试里 12 个关键用例
    与 7 个类。**「说清了为什么」的那批高质量注释一律原样保留**，没有重写。
23. **文档按现在的链路重写**：README 的链路图/地址表/目录树/抓取频率（GitHub 现在只有 `rss.yml`
    每天 3 次，`minute.yml` 与 `nightly.yml` 的定时已停用）；`OPERATIONS.md` 的「刷新节奏」
    重写成两张表（GitHub 与树莓派各自的抓取/显示节拍）、§15 排障改指向 `moreaix.com/rss/rotate.json`；
    `Pi/README.md` 补上 `rotate_screen.py` 与 `rss-rotate.*`（显示时间线与抓取解耦是核心机制，
    原来完全没写）；`Pi/TUNNEL.md`、`Pi/KINDLE.md` 按实际路径/版式/灰度改。
24. **页面修了一个线上可见的错**：`docs/index.html` 的「设备版地址」与 `docs/screen.html` 的预览
    都还在读 `cheney-hub.github.io/rss-hub/feed-wrap.xml` —— 那个文件已随设备版下线（404），
    预览页只会显示「无法加载」。现在两处都指向 `moreaix.com/rss/feed-wrap.xml`，
    预览页跨域直接读（Pi 的 `serve.py` 对所有响应都发 `access-control-allow-origin: *`）。
25. `docs/feed.xml` 重新生成（此前那次备份停在 1.4.0，`<generator>` 与版本对不上）。

**基线**：`tests/golden.txt` 重生成，改动只有 2 行（第 7 条的标题尾巴）+
「`pubDate` 原文」新增样本 4 行（锁住「`date_raw` 优先于 `ts`」这个语义 —— 以前把来源从源站原文
改成 `ts` 现算，基线逐字节不变、112 条用例全绿，等于这个语义没人看住）。

## [1.7.2] - 2026-10-31

1.7.1 已推送并在 Pi 真机上跑起来了（等于已发布），所以这批收尾**单独发一版**：其中两条是
行为变更（判重门槛回退、`norm_title` 截断放宽），混在 1.7.1 里就没法用 feed 的 `<generator>`
判断 Pi 上跑的到底是哪一版。六项，逐条「改了什么 + 为什么」：

1. **判重门槛 6 回退 8（`render.py` 的 `DEDUP_TITLE_MIN`）**：修「三源同稿同时上屏」那个 bug
   只需要「保留数字」这半边 —— 那条标题归一化后是 **9 字**，8 字门槛照样拦得住，门槛本来不用动。
   在 Pi 线上真实 120 条上量过：门槛取 6 / 7 / 8 **丢的条数都是 4**，降到 6 一条都没多丢；
   但 6 会新增一类误并：栏目型短标题（主力资金流向、融资融券余额、北向资金播报、今日重要公告、
   新股上市提示、盘中热点追踪、隔夜外盘综述，归一化后 6~7 字）不同事件撞名就被并成一条。
   而 `dedupe()` 作用在**整份 items** 上（先去重再截 `ITEM_LIMIT`），误并 = 完整版 feed.xml
   直接少一条，比多显示一次重复更贵。理由写在常量注释里；`tests/test_dedupe.py` 用
   「8 字判重 / 7 字不判重」**两条边界**锁住，且**不写死数字**（断言语料长度 == 门槛 / 门槛 - 1，
   门槛一改就红）—— 项目里 `ROTATE_MAX_LAG`、`sim --seed` 都栽在「参数改了没人能证明」上。
2. **`norm_title()` 截断 20 → 40 字**（新常量 `NORM_TITLE_MAX`）：只比前 20 字时，两条不同新闻
   可能**前 20 字完全相同、分歧落在第 21 字之后**而被当成同一条丢掉一条 —— 线上真有这种
   （「…三季度增仓346.71万股，持股比例**升**至5.2%」vs「…**降**至4.2%」）。
   键加长是**纯收紧**方向：只会让撞键更难，不会新增误并。
3. **`recent` / `shown` 类型兜底（`rotate._str_list()`）**：不是 list / tuple 就当这个字段不存在
   （字符串也挡：它可迭代，会被逐字符拆成好几个键）。rotate.json 是**发布出去的产物**
   （CDN 缓存旧版、别的分支写过、手改坏了都可能），`{"recent": 12345}` 在列表推导上是 TypeError，
   而 `build_feed.main()` 没有 try —— 整个这一轮会死在选条之前，feed 与 rotate.json 都不落盘，
   屏幕就此冻在上一版（和「设备坏了」在用户眼里分不出来）。代价只是重复播一条。
4. **README 用例数 / 目录树对齐 + 门禁新增用例数校验**：README 树里仍写着「94 个用例
   （core 64 / rotate_edge 29）」、且没有 `tests/test_dedupe.py`，同一页另一处却写着 106 ——
   一页自相矛盾。现在树补齐两个新文件、总数改 **112**（core 64 / rotate_edge 39 / dedupe 4 /
   state_hygiene 4 / golden 1），并由 `tools/review_check.py` 新增的 `check_test_count_sync`
   **按 `unittest` 加载器实际数出来的数核对**（只加载不执行，几毫秒），写错 = 必改。
5. **补测「屏显串撞车的同窗多条被往后挪、而不是被丢」**：
   `tests/test_rotate_edge.py::test_colliding_titles_are_pushed_apart_not_dropped` ——
   池子 `B0-old` / `B0-new` / `B1`（前两条显示键都塌成 `B0`），断言三条**各上屏一次**、两个 `B0`
   **不相邻**。它锁的是 1.7.1 改名时被抹掉的那条路径，理由见 [1.7.1] 的追溯说明。
   另新增 `tests/test_state_hygiene.py`（4 例：坏形状不崩 + 正常值仍生效），单独成文件是为了
   不让 `test_rotate_edge.py` 越过 750 行的拆分提醒线。
6. **文档口径**：「最近 3 窗不重复」记的是**已经播掉的 w-1 / w-2 / w-3**，本窗正在选的那条不在
   里面，所以同一个显示串之间真正的最小间隔是 **4 窗 = 20 分钟**（不是 15 分钟）。
   已同步 `rotate.py` 的 `RECENT_MAX` 注释、`OPERATIONS.md` §15 字段表与症状表、README 轮播一节。

基线 `tests/golden.txt` 重生成：**10 行变化**（相对 1.7.0 累计 11 行），全在
`strip_dateline / norm_title` 那一节（键取满 40 字，例如
`key='国机精工第二大股东国新投资有限公司三季度增仓34671万股持股比例升至52%'`）；
`split_title` / `compose_body` / 两份 RSS 的各段**逐字节未变** —— 显示规则没动，
动的只是「两条是不是同一条」的判定。

**升级注意**：`ROTATE_STATE_VERSION` 仍是 4、状态字段没变，1.7.1 的 `rotate.json` 可以直接沿用，
不会清标记重开一轮。

## [1.7.1] - 2026-10-31

修「屏幕上一直在显示」的真因 —— 从 Pi 线上 `https://moreaix.com/rss/feed.xml`（120 条）统计
**屏上真正显示的前 13 字**，发现 12 条分属 4 组显示串完全相同：

- **同一条新闻被多源转发**：「国际油价30日上涨」同时出现在 雪球·实时快讯 / 财联社·电报滚动 /
  金十数据·7×24，是同一条稿子被三家转发，用户看到的「一直在显示」就是它；
- **不同新闻但前缀撞车**：自由时报《TAIPEI TIMES》×4、金十「美国商品期货交易委员会（CFTC）：截至…」×3、
  第一财经「A股三季报风云丨2026年…」×2 —— 标题不同，但截到 13 字后屏上一模一样。

两条改动，一条管内容、一条管观感：

1. **跨源同题去重（`render.py`）**：`norm_title()` 不再删数字与字母
   （原来那句 `re.sub(r"[a-zA-Z0-9]", "", t)` 把「国际油价**30**日上涨」变成「国际油价日上涨」= 7 字，
   够不着当时的 8 字门槛，于是三源同稿一条都没拦住）。数字是标题身份的一部分：
   「30 日上涨」和「31 日上涨」是两条新闻，删数字会在同一天里批量误并。
   门槛提成模块级常量 `DEDUP_TITLE_MIN = 6`（不再写死 8），并补了「不同取值 → 不同结果」的反向用例。
   （门槛在 [1.7.2] 回退到 8：实测降到 6 一条都没多丢，却新增一类栏目型短标题的误并。）
2. **显示端防连播（`rotate.py`）**：新增 `RECENT_MAX = 3` —— 最近 3 窗（15 分钟）内不重复显示
   **屏显串相同**的条目（用 `render.split_title()` 取屏上那一行，不自己截，免得跟显示规则漂移）。
   这是观感约束、不是内容去重：**被跳过的条目不写 `shown`**，保持未标注，稍后照常上屏，内容不丢；
   候选全在 `recent` 里时退回最新那条 —— 宁可连播也不空屏。状态新增 `recent` 字段（≤3 条，
   最近的在前），旧状态没有这个字段按空列表处理，所以 `ROTATE_STATE_VERSION` 仍是 4。

基线 `tests/golden.txt` 重生成后 diff **只含 7 行**，全在 `strip_dateline / norm_title` 那一节
（去重键里多了数字与字母，例如 `key='净亏损2015亿元同比由盈转亏'`）；`split_title` / `compose_body` /
两份 RSS 的各段**逐字节未变** —— 显示规则没动，动的只是「两条是不是同一条」的判定。

### 1.7.1 的追溯说明：一条被改名抹掉的测试路径

`tests/test_core.py::test_same_span_items_each_get_a_turn`（约 280-297 行）的假标题原为
`B0-old` / `B0-new`，加 `RECENT_MAX` 之后它挂了：**`render.split_title()` 会去掉
「- 来源名」这类尾巴，两条的显示键都塌成 `B0`**，于是第三条被插到两个 `B0` 中间，
断言的出场顺序不成立。当时的处理是把语料改名成 `B0先到` / `B0后到`（避开撞车，
这条用例要测的「同一时间片里几条都能轮到」仍然是它），但**「显示串撞车 → 第二条往后挪」
这条路径就此没人守了**。所以：

- 这个改名与它的后果记在这里，免得下次有人看到 `B0先到` 不知道为什么叫这个；
- 新行为（同窗多条若屏显串相同，第二条往后挪最多 3 窗 = 15 分钟，内容不丢）已写进本条
  第 2 点和 `OPERATIONS.md`（「3 窗」的实际效果 = 同一显示串最短间隔 4 窗 = 20 分钟，
  见 [1.7.2] 第 6 条）；
- 这条路径在 [1.7.2] 由 `tests/test_rotate_edge.py::test_colliding_titles_are_pushed_apart_not_dropped`
  锁回：池子用 `B0-old` / `B0-new` / `B1`，断言三条各上屏一次、且两个 `B0` **不相邻**。

## [1.7.0] - 2026-10-31

**轮播从「按时间片」改成「按条」——修一个让大半条目永远上不了屏的规则缺陷。**

**同一版起，GitHub Pages 不再发布设备版**：三个 workflow（rss / minute / nightly）都把
`SCREEN_OUT` 改指 `${{ runner.temp }}/feed-wrap.xml`，仓库里 tracked 的 `docs/feed-wrap.xml`
已删除。仓库与 Pages 只保留完整版 `feed.xml` 给阅读器订阅；设备版由树莓派发布在
`moreaix.com/rss/feed-wrap.xml`，Dot App 订阅那一个。原因是设备版要「每 5 分钟换一条」，
而 GitHub 侧只有每天 3 次定时构建，节拍对不上，不做这个切分就会让设备一小时甚至几小时不动。

旧规则（`rotate.py` 1.4.0 起）：把池子按 `ROTATE_SECONDS`(300 秒) 切成时间片、**每片只取最新一条
当代表**，每窗播「全池未播过里最新的一片」，`shown` 存片号。缺陷是同一个 5 分钟片里后到的那些条目
**既拿不到代表位、也永远不会被标注**，等于进池即弃 —— 实测池子 120 条只切出 26~44 片，一天里
大半条目从未获得上屏机会。这跟当初「防止老条目轮不到」的初衷正好相反。

新规则（用户原话「已显示的标注后，重新显示最新的未标注信息」）：

- 池子 = 最新 `ROTATE_POOL_MAX`(120) 条，按发布时间倒序；
- 每窗显示「**尚未标注的最新一条**」，显示后**用 guid 标注**它；
- 全部标注完 → 清空标注、从最新一条重开一轮（`cycle + 1`）。

为什么用 guid 而不是索引：池子每窗都在滚动（新条目从头部进来、旧的被 `ROTATE_POOL_MAX` 挤出去），
序号会错位；guid 跟着条目身份走，滚到哪都不会把 A 的票记到 B 账上。`shown` 因此从「片号列表」
变成「guid 列表」，状态结构升到 `ROTATE_STATE_VERSION = 4`（旧状态宁可丢掉重开一轮，也不让
两种口径的标记混在一起造成乱序）。

保留下来的行为（改坏了就是回归，都有用例钉住）：

- 同窗幂等（`w = now // ROTATE_SECONDS` 不变时不推进），但内部改成**按 guid 找回屏上那条**；
  副作用是同窗内新到的更晚条目**不再顶掉屏上内容**（旧机制的「片内代表更新」随代表概念一起退役）。
- 未来时间戳（源时钟错乱）仍不参与选条；解析失败 `ts=0` 的仍排最末、照常参与。
- `dwell` 变了 / `window` 超前超过 `CLOCK_SKEW_WINDOWS`(2) 仍丢状态重开；
  `load_rotate_state()` 仍取本地与线上里 `window` 更大的一份。
- 新一轮仍跳过「刚上屏那一条」起轮，所以不会出现同一条连显两窗。

状态字段改名（排障入口 OPERATIONS.md §15 同步更新）：`bucket`（片号）→ 无；
`lag`（落后几片）→ **`back`（落后最新几条）**；`at` 从「片对应的时间」改成「**这条的发布时间**」；
`pool` 从「可播片数」改成「**可播条数**」。`_pick_bucket()` 也随之改名为 `_pick_item()`（语义变了，
留旧名只会让人以为还在切片），`build_feed` 的 `__all__` re-export 同步改名。

仿真（6 种子均值，`tools/sim_rotate.py`）确认这次是双赢，不是「新鲜度换覆盖率」：
最差延迟 **4.4 小时 → 43 分**，中位 2 分不变，命中最新 91%，落后 ≥2 条占 2%（原 10%），
覆盖率 34%（按片轮是 32%，同水位 —— 受限于「每窗只播 1 条」这个物理上限，不是这次的取舍）。
尾巴的来源没变：延迟最大的 10 个窗仍然是「本窗新增 0 条」的时刻。

行为基线 `tests/golden.txt` 只动了 `rotate_pick 序列` 那一行（第三列从「落后几片」变成「落后几条」，
标题序列一字未变 —— 样本每条占一片，改动前后同一格局）；渲染层各段逐字节未变。

## [1.6.1] - 2026-10-30

接受「GitHub cron 低频兑现」的现状（2026-10-30 实测：12 小时只给 4 档，间隔 1.5~3.5 小时，
19/20/22/23/00/02/03 点的档位整档跳过），把文档口径从「每 5 分钟一版」改成如实的
「设计 5 分钟、实际约 1~3 小时」，并修掉这个现状带出来的一个真缺陷：

- **每日备份原先钉在「UTC 00 点那一轮」**，而 00:07 档最容易被 GitHub 整档跳过 ——
  10-30 当天就因此一次都没备份（仓库里的 `docs/feed.xml` 停在前一天）。
  改成按日期判断：`git log -1 --date=format:%F -- docs/feed.xml` 不是今天（UTC）就提交一次，
  当天第一档跑起来的那轮负责备份，之后各轮自动跳过。被跳档不再等于不备份。
- 文档：README 的「抓取频率 / 发布频率」两行与「已知限制」第一条、OPERATIONS 的「刷新节奏」
  与 §10（补第三次实测：11.4 小时后开始触发、兑现率与跳档明细）、页面副标题，全部改成实测口径；
  `docs/screen.html` 的「每 5 分钟换一条」保留——那说的是设备轮播节拍，不是入库速度。

输出逐字节不变（只动 workflow 逻辑与文档，行为基线未重生成）。

## [1.6.0] - 2026-10-30

**channel 增加 `<copyright>`**：`内容版权归各来源媒体，本站仅聚合标题与摘要，原文以各条 link 为准`。
条目一级本来就有 `<source>`（源名 + 源地址）与指向原文的 `<link>`，缺的是「整份聚合归谁」这句话——
聚合别人的标题与摘要而不写版权归属，是一处该补的缺口。

改动只有 `render.py` 的一处常量 + 一行输出，两个版本都带（设备版一版只发一条，这几十字节无所谓）。
行为基线因此**只多两行**（设备版 / 完整版的 channel 各一行），其余逐字节不变。

被否掉的两个方案（用户 2026-10-30 决定）：设备屏上点缀源名、完整版标题加【源名】前缀——前者要
从 90 全角的正文预算里再挤 3~8 字，后者破坏标题原样，收益都抵不上损失。

## [1.5.0] - 2026-10-30

一次全量代码审查（正确性 / 可读性 / 架构 / 安全 / 性能五轴），修 4 个缺陷、补 6 个用例、
清掉一批会误导人的过期文档。渲染层的行为基线 `tests/golden.txt` **逐字节未变**——
唯一会改变输出的修复（`clean_text` 的段落边界）刻意只作用在「两个半角词之间」，
中文排版一个字都没动。

四条缺陷的性质不同，按危害排：**说明页 XSS**（安全，真实可利用）>
**抓取无响应体上限**（健壮性，源地址改错就能打爆内存）>
**空 `<link></link>`**（RSS 合法性，会让阅读器显示死链）>
**段落边界粘词**（通用函数的正确性，当前 49 个源未触发，见下）。

### 修复

- **段落边界把英文单词粘成一个词**（`render.py::clean_text`）：旧实现把 `<[^>]+>` 一律删掉，
  `<p>Hello world</p><p>Second</p>` 会变成 `worldSecond`、`jumped<br>Apple` 变成 `jumpedApple`。
  现在段落/换行标签先打标记，再判断边界两侧是不是半角词字符：是就换成空格，不是就照旧删掉。
  为什么只认半角：汉字之间本来就挨着排，无条件补空格会给中文正文塞进排版噪音；
  `&nbsp;`（源站主动要的空格）也不受影响。基线逐字节未变即此项只修了缺陷、没动既有排版。
  **实测口径（别把它说成线上可见的 bug）**：当前 49 个源里有 16 个存在「段落标签紧贴」的写法，
  共 4463 处，但这些位置的字符全是汉字，所以线上文本**今天没有可见变化**；
  用新旧两份实现跑 245 条真实摘要，输出差异为 **0 条**。这是一处通用函数的正确性修复 ——
  源一旦换成英文分段内容（或补进一个海外媒体的中文版以外源），旧实现就会粘词。
- **说明页存在 XSS**（`docs/index.html`）：条目标题 / 链接 / 源名来自第三方 RSS，
  旧代码用 `innerHTML` 拼模板，任一家源在标题里写 `<img onerror=…>` 就能在本页执行脚本。
  改成逐条创建 DOM 节点 + `textContent`，链接只接受 `http(s)`（挡掉 `javascript:` 伪协议）。
- **`feed.xml` 写出空 `<link>`**（`render.py::_item_lines`）：源里没有链接时旧代码会输出
  `<link></link>`，阅读器把它当死链。RSS 2.0 里 `<link>` 本就可选，改为缺字段就不发。
  同一处把 `it["link"]` / `it["title"]` 的直接取键换成 `.get`，回退条目少字段时不再 KeyError。
- **抓取没有响应体上限**（`build_feed.py::http_get`）：源地址被改成巨型文件时一次 `read()`
  就能吃光 runner 内存、整轮采集跟着崩。加 12 MB 上限，压缩前后各查一次（解压后的体积才是真占内存的量），
  超限只让这一个源失败并走回退。

### 清理

- 删掉 `_channel_lines()` 里从未被使用的 `total` 形参；`build_feed.py` 用 `__all__` 声明
  兼容再导出的名字（去掉没人用的 `_bucketize`），静态检查不再有「导入了却没使用」的噪音。
- `tests/test_core.py` 修掉未关闭的文件句柄（`open().read()` 触发 `ResourceWarning`）
  与模块级重复导入的 `re`。
- `.github/workflows/rss.yml` 的 `push.paths` 补 `rotate.py`：改轮播规则原本不会触发即时重建，
  要等下一个整点。
- `review.yml` 的语法检查从 `build_feed.py` 扩到根目录全部 `.py`（`rotate.py` / `render.py`
  之前不在这道检查里）。

### 新增

- **`TestCleanText`（5 例）**：英文段落边界必须留空格、汉字之间必须不留、`</p>\n<p>` 不叠成两个空格、
  `&nbsp;` 保留、CDATA 与实体。
- **`test_handwritten_workflows_carry_all_tuned_params`**：把「workflow 参数扫到就比」升级成
  「生成器模板里的每个参数，`rss.yml` / `nightly.yml` 都必须写出来且值相同」。
  少写一项不会报错，只会悄悄落回代码默认值，等默认值一改那条路径就与主线不一致了。
  已用「删掉 nightly.yml 的 `ROTATE_POOL_MAX`」证伪过，用例确实会红。
- 用例总数 88 → 94。

### 文档

- 修掉一批与代码对不上的描述，都是会直接把排障带偏的那类：
  - README 两处仍写「默认不限条数，全部进 feed」（1.4.0 已改回 120 条）；
  - README 的链接去重规则仍写「去掉 `#` 与 `?` 参数再比」（1.3.0 已改成只剥追踪参数）；
  - OPERATIONS §15 仍说「完整版通常 220+ 条，`items=120` 是正常的」（现在两边都是 120）；
  - OPERATIONS §13 说「三个工作流」却只列了两个；
  - OPERATIONS §8 的编码顺序、§7 的阈值所在模块（`build_feed.py` → `render.py`）；
  - `feeds.txt` 文件头仍写「每分钟一次」（实际是每 5 分钟）；
  - CODE_REVIEW 的架构轴仍写「排版逻辑只能在 `build_feed.py`」，与三层拆分直接冲突。
- README / OPERATIONS / CODE_REVIEW 里的用例数、行数、cron 表达式、单轮耗时改成就地准确的说法；
  容易漂移的数字（如生成文件行数）改成不写死。

## [1.4.0] - 2026-10-30

用户明确要求：**总条数 120 不变，按时间排序后截取最新 120 条**。1.3.0 把 `ITEM_LIMIT` 放开成 0
（不限条数）过头了 —— 那条「不要再截断」指的是**正文**，不是条数。

### 变更

- **`ITEM_LIMIT` 默认 0 → 120**：去重后按 `pubDate` 倒序，取最新 120 条。
  排序在前、裁剪在后（`run_once` 里 `items.sort()` → `dedupe()` → `items[:ITEM_LIMIT]`），
  所以砍掉的一定是最旧的那批，不是随机截断。`ITEM_LIMIT=0` 仍表示不限，随时可回。
- **正文不截断保持不变**：`FEED_BODY_MAX` 仍为 0（全文照发），两条限制各管一头，互不牵连。
- **设备池不动**：`ROTATE_POOL_MAX` 仍 120。两个参数现在是同一个数量，但职责不同
  —— 完整版截 120 条给阅读器、设备池取 120 条做轮播；改任何一个都不会顺手改到另一个。
- 4 处环境变量同步（`tools/gen_minute_workflow.py` 模板 → 重新生成的 `minute.yml`、`rss.yml`、`nightly.yml`），
  `tests/test_core.py::TestWorkflowParams` 会扫工作流里的显式值与代码默认值比对，漏一处即红。
- 预期效果：`feed.xml` 从约 991 KB / 221 条回到约 464 KB / 120 条（本地实跑：49 源全成功、去重后 125 条、
  裁到 120 条、耗时 39 秒），轮播上界不变（4.3 小时）。

## [1.3.1] - 2026-10-30

只改运维节奏，**输出逐字节不变**（渲染层一行未动，`tests/golden.txt` 无需重生成）。

### 变更

- **`docs/feed.xml` 的 git 备份从约 40 次/天降到 1 次/天**（用户要求：「不需要这么多备份」）。
  旧规则在每 10 轮（第 1、11 轮）提交一次，白天 18 小时 × 2 = 36 次，加夜间合计约 40 次。
  新规则只在 **UTC 00 点的首轮**（北京 08:00）提交，一天一条。
  - 为什么可以降到一天一次：Pages 走 artifact 部署，**线上那份才是权威副本**，仓库副本只用于
    「Pages 挂了从 git 取回」。每 5 分钟提交一次只是把 `git log` 刷满几乎相同的快照。
  - 代价：仓库里的 `feed.xml` 最多落后 24 小时。判断线上是否最新**一律看 Pages**，
    不要再拿仓库文件当证据（1.3.0 刚踩过：仓库副本停在旧版，被误判成「改了没生效」）。
  - 这一轮若被 GitHub 拖过整点，当天就不备份，次日恢复 —— 允许缺口，不值得为它加状态机。
- **拉代码从「每 10 轮」改为「每小时首轮」**（`CYCLE -eq 1`）。这条不是顺手改的：
  原来的 `git pull` 藏在备份分支里，备份降频后会连带把「代码生效延迟」从 50 分钟拖到 24 小时。
  必须把「拉代码」和「备份」拆成两个条件。顺带把 pull 挪到 `build_feed.py` **之前**，
  同一轮就用上新代码（原来 pull 在构建之后，要等下一轮才生效）。
  pull 前先 `git checkout -- docs` 丢弃上一轮的未提交产物，否则 tracked 的 `feed-wrap.xml`
  会让 `git pull --rebase` 报 “You have unstaged changes” 而永远拉不动。
- `tools/gen_minute_workflow.py` 模板与文件头注释同步，`minute.yml` 重新生成（非手改）。

## [1.3.0] - 2026-10-29

用户三项诉求一起落：**每源 3 → 5 条**、**完整版（feed.xml）正文与条数不再截断**、
**新增 6 个中国台湾 / 日本中文源**。属于次版本级行为变更（输出内容显著变化），不是补丁。

### 变更

- **每源取数 3 → 5**（`PER_SOURCE` 默认值）：源总数 43 → 49，去重后池子约 200~245 条。
  `tools/gen_minute_workflow.py` 模板同步，`minute.yml` 重新生成（非手改）。
- **完整版正文不再截断**（`FEED_BODY_MAX` 默认 300 → 0）：用户原话「feed.xml 内获取的内容不要再截断」。
  `compose_body()` 本就支持 0 = 不限，只改默认值；想防单条摘要撑大文件仍可显式设正数。
  实测单份 `feed.xml` 体积从约 147 KB 涨到约 1.0 MB（228 条、随源内容浮动）—— 阅读器按需拉取，可接受。
- **完整版条数不再截断**（`ITEM_LIMIT` 默认 120 → 0，`max(1,…)` 改 `max(0,…)`，裁剪处加 0 守卫）：
  真正的规模上界变成 源数 × `PER_SOURCE`（现 49×5=245）。
- **链接去重改「归一化」**（`render.py` 的 `dedupe()` → `_link_key()`）：旧规则把链接 `?` 之后
  一刀砍掉，把整个查询串当追踪参数；而 RTI 的条目 id 恰恰写在查询串里
  （`/news?uid=3&pid=240741`），导致该源全部条目折叠成同一个键、每轮只剩最新 1 条。
  新规则只剥 `#` 锚点与追踪参数（`utm_*` / `spm` / `from` / `ref` 等），**保留有意义的查询参数**，
  且参数排序后比较（顺序不影响身份）。影响面已核对：旧 224 条 → 新 228 条，唯一变化是 RTI 1→5，
  其余源一条不动。标题去重规则未动。
- **新增 6 个源**（逐一实测可达、可被本项目解析器解析）：
  - 中国台湾（繁体）：自由时报·即时（19 条可解析）、自由时报·财经（5 条）、
    中央广播电台 RTI·全部（5 条，207 条大源）；
  - 日本（简体）：日经中文网·经济 / 金融 / 中国（各 10~12 条）。
    日经「经济」与「最新」约 40% 重叠，由现有跨源去重吸收，无需特殊处理。

### 关键配套：`ROTATE_POOL_MAX` 默认 0 → 120

完整版放开条数后，若设备轮播池跟着不限（=245 条），**最差滞后 = 池条数 ÷ 每窗新增**
会从 51 片 ≈ 4.3 小时推到 100+ 片 ≈ 8 小时以上，设备端体验直接退化。
所以设备池显式钉在「最新 120 条」：完整版诉求（全文全量）与设备端诉求（别太旧）由两个参数各管一头
—— `ITEM_LIMIT` 管完整版、`ROTATE_POOL_MAX` 管设备池。**从此「最差滞后」由 `ROTATE_POOL_MAX`
决定而不再由 `ITEM_LIMIT` 决定**，README 已改写对应段落；轮播算法本身一行未动。

### 基线漂移（预期内，仅快照表头）

`tests/golden.txt` 用 `tools/golden_snapshot.py --write` 重新生成（未手改）。实际漂移只有
1 行：快照表头跟随默认值，`FEED_BODY_MAX 300` → `0`；**行为基线本身字节一致** ——
固定输入上的样本正文都不超过旧的 300 全角上限，正文上限放开并不改变它们的输出。

### 文档与测试

- 单测 83 → 87：`test_summary_truncated_by_feed_body_max` 改为显式传 `body_max=300`
  （不再跟随 `R.FEED_BODY_MAX` 默认值 —— 测试不该随调参漂移），并新增 `body_max=0` 时
  900 字原样返回、不出现省略号的用例；链接归一化补 3 例（有意义查询参数不折叠、
  追踪参数不构成身份、参数顺序无关）。
- README / OPERATIONS / CODE_REVIEW / 两个页面的所有旧口径（43 源、每源 3 条、120 条上限、
  300 全角）逐条改为新口径；README「最差滞后」段改写为按 `ROTATE_POOL_MAX` 计算。
- 版本三件套同步：version.py → 1.3.0，README / OPERATIONS 顶部、两个页面页脚、
  `build_feed.py --version` 示例输出。

## [1.2.3] - 2026-10-29

收尾清账：**代码行为零变化**（83 条单测全绿、golden 基线零漂移），只删死代码、把文档里
说错的数改对、并去掉一个不再需要的定时监控。

### 删除

- `tools/review_check.py` 的 `RANK` 常量：定义之后从未被引用（排序用的是报告里的追加顺序，
  展示用的是 `LABEL`）。留着会让读代码的人以为存在「按严重度排序」这回事。
- 仓库外的定时监控任务（每 3 小时一次的链路健康检查）已停用删除 —— Actions 与 Pages 的
  状态页本身就是权威信息源，README / OPERATIONS 的「监控」一节写的正是去哪里看，不需要第二个哨兵。

### 文档校准（都是「说过头」或「说旧了」）

- 单测数量 **39 → 83**：README 的「开发与审查」和 CODE_REVIEW 的 H4 规则里都还写着 39，
  这是 1.1 时代的数；1.2.0 起轮播边界用例补齐后是 83（`tests/` 三个文件）。
- 「单轮耗时 40 秒左右」→ **本机约 10 秒、GitHub runner 上 25～45 秒**。原数字既没有口径也不准：
  本机真跑是 10.3 秒，CI 侧「改源即时发布」整条作业（含 checkout 与部署）25～53 秒。
- 提交前铁律 2 的范围从 `build_feed.py` 扩到 **`build_feed.py` / `rotate.py` / `render.py`**：
  1.2.2 之后轮播不在 build_feed 里了，只写一个文件名会让人以为改 rotate.py 不用停旧作业。
- `review.yml` 顶部注释里的用例数同步为 83。
- **铁律 2 的描述改准**：原文说跑着的作业「用的是它启动时 checkout 的旧代码」，实际是
  「上一次备份轮 `git pull` 到的代码」——`minute.yml` 只在 `CYCLE % 10 == 1` 那一轮拉取，
  所以最长 50 分钟才更新，不是永久锁死。同时补上 `OPERATIONS.md` 新增的 §16：怎么查有没有
  在跑、三种取消方式（网页 / `gh run cancel` / 一次 API 调用）、取消有 20 秒~5 分钟的延迟、
  取消后如何手动补一轮单次构建。这一段是踩了坑才写出来的：上一轮判断「没有 token 无法 cancel」
  是错的，git 的凭据管理器里就存着一个带 `repo, workflow` 权限的 PAT，取出来直接打 API 即可。

## [1.2.2] - 2026-10-29

纯结构重构。**抓取、渲染、轮播选择全部逻辑一行未改，输出逐字节不变**（golden 基线零漂移，
83 条单测全绿），改的是代码摆放位置与几处措辞。

### 重构

- **把轮播从 `build_feed.py` 拆成 `rotate.py`**（834 行 → 555 行；搬出去的 `rotate.py` 334 行）。
  拆分动机是职责而非行数：`build_feed.py` 同时装着「抓取/回退/写文件」和「池子怎么切片、
  这一窗播哪条、游标状态长什么样」两件事，改轮播要在 800 行里找，QA 写用例也要先穿过一堆
  HTTP 代码才看得到纯函数。现在按 render.py（怎么显示）/ rotate.py（播哪条）/ build_feed.py
  （抓到什么、写到哪）分。
  搬到 `rotate.py` 的有：`rotate_pick`、`_pick_bucket`、`_pickable`、`_bucketize`、`_bucket_of`、
  `_trim_shown`、`_hold_current`、`_build_state`、`_advance_note`、`ROTATE_STATE_VERSION`、
  `CLOCK_SKEW_WINDOWS`、`ROTATE_SECONDS`、`ROTATE_POOL_MAX`、`load_rotate_state`、`save_rotate_state`；
  留在 `build_feed.py` 的只有「状态文件在哪、线上地址在哪」（`ROTATE_STATE`、`_rotate_url`）——
  这两项跟着 `OUT_FILE` / `FEED_URL` 走，属于本文件的配置。
- `load_rotate_state(state_path, remote_url, user_agent)` / `save_rotate_state(state, state_path)`
  改成收显式路径。此前它们读模块内的 `ROTATE_STATE`，搬到新模块后若要沿用，就得在 rotate.py
  里复制一份 `OUT_FILE` 的默认值解析——**默认值复制两份早晚会漂移**，所以改成由调用方给。
- `build_feed.py` re-export 了 `rotate_pick` / `_pick_bucket` / `_bucketize` / `ROTATE_SECONDS` /
  `ROTATE_STATE_VERSION` / `CLOCK_SKEW_WINDOWS`，让 `tests/` 与 `tools/`（`sim_rotate.py`、
  `golden_snapshot.py`）继续按 `build_feed.xxx` 调用，这一轮不需要改任何测试文件。
  这是**过渡期的兼容层，不是新家**：新代码请直接 `from rotate import ...`。

### 文档

- README 目录结构补上 `rotate.py`；OPERATIONS 顶部「常量契约」的归属改成
  `build_feed.py` / `rotate.py`（`ROTATE_*` 现在由 rotate.py 读环境变量）。
- 删掉 `build_feed.py` 里已随代码搬走的 `import json`（`_read_json` 跟着去 rotate.py 了）。

## [1.2.1] - 2026-10-29

1.2.0 上线后的复核修正。**轮播代码一行未动，行为不变**，改的是文档与仿真工具。

### 修复

- **`tools/sim_rotate.py --verbose` 看不到尾部**：它跑 `hours=4`，而 `hours` 从 `t=0` 起算，
  40 个窗只覆盖 0:00–3:20 —— **全是夜间**。夜间恰好是全天最健康的一段，于是 `--verbose`
  永远展示不出真正的尾部，看着像「最差 7 分」。改成跑满 24 小时、只打延迟最大的 10 个窗，
  并带出「本窗新增几条」，让人一眼能看出尾部是不是落在空窗上。
- **README 的 p90 区间是循环论证**，而且 **p90 本身就不该用来描述这条尾巴**：延迟分布在
  6~60 分之间几乎是空的（只占 3%），90 分位正好落在这条缝上，单种子在 4~75 分之间乱跳。
  已改成三段描述：90% 的上屏在 6 分内 · 7~9% 在 30 分以上（这批中位 2 小时）· 最差 4~6 小时。

### 更正（1.2.0 条目里的两处错误归因）

1.2.0 把尾部写成「夜间积压、早上补播」。20 个种子按「本窗有无新鲜未播片」分类后，
**尾部 100% 来自零产出的窗**，与昼夜无关；夜间反而是全天最健康的一段（中位 1.6 分、最大 13 分），
因为夜间一小时才构建一次、每次抓回一小时的产出，「最新的未播片」总是刚发布的。

更深一层：**「最差能多旧」的上界是 `ITEM_LIMIT` 换算成时间，不是夜间**。
120 条 ÷ 每窗 2.35 条 = 51 个时间片 ≈ 4.3 小时，实测 6 种子均值 4.35 小时。
「池底年龄」下午（中位 284 分）比早上（257 分）还深 —— 夜间能让池子满，但白天它本来就一直是满的。
所以这个上界调 `ROTATE_SECONDS` 也没用，**唯一能调的是 `ITEM_LIMIT`**：调小直接缩短上界，
代价是池子里的历史更少。这条已写进 README。

## [1.2.0] - 2026-10-29

轮播机制重写。旧机制是「开轮时拍一张时间片清单，从新到旧一格一格推」，等价于
**第 k 窗播第 k 新的那条** —— 延迟随轮次线性增长，而且冻结的清单里没有运行期间新涌入的条目。
新机制只有一条规则：**每窗播池子里最新的、还没播过的那一片；全播过才清标记重开一轮。**

### 修复（都属于「不报错、只是结果不对」）

- **上屏内容单调变旧**。线上实测：`window=5977601`、`bucket=5977586`，落后 15 片 = 75 分钟，
  屏幕上是 12:10 的消息而当时已 13:25；完整版最新三条是 21:20，设备版在播 20:13 的。
  24 小时仿真：延迟中位 120 分、p90 230 分、最差 4.3 小时，**只有 6% 的上屏是当时最新的一条**。
  改为锚定当前窗后，中位 1 分、命中最新 86%（同一套仿真）。
- **运行期间的新内容整轮播不到**。清单是快照，新条目不在里面，一轮 4 小时里一条都上不了屏。
- **池子小、换血快时会重播刚上屏那条**。重开一轮时 `keys[0]` 往往正是刚播过的片，
  清标记后立刻又选中它。默认 `ITEM_LIMIT=120` 下实测 1~2 次/天，小池子 400 窗里 8~11 次。
  现在重开时避开 `state["bucket"]`；池子只剩一片时只能重播（无解，但不崩）。
- **源时钟错乱造出的未来时间戳会霸屏**。片号恒为最大，「最新未播片」永远是它，`lag` 变成
  巨大负数（页面会显示「滞后 -402000 分」）。改为**发布时间未到的片不参与选条**；
  `hold` 仍看全量片，否则时钟回拨后屏上那条的片号显得「在未来」，会被误判成离场。
  注意：不能在解析层「钳到抓取时刻」——抓取每 5 分钟重跑，钳值跟着往前跑，同一条每窗重播
  （实测连跑 6 窗全是它），比不钳更糟。
- **状态里的 `window` 是未来值时永久卡死**。`w > prev_w` 永远为假 → 一直 hold → `window`
  永不前进，而且每次运行都原样读回这份坏状态，**自愈路径不存在**。加 `CLOCK_SKEW_WINDOWS = 2`
  闸门：容忍 2 窗（10 分钟）以内的回拨（NTP 校正会让墙钟往回跳几十秒），超了整份丢弃重来。
- **并发抓取导致同一时间戳的次序不定**。条目是 `as_completed` 收上来的，数组初始顺序取决于
  各源返回快慢，`sort` 又是稳定排序，所以同一时间片的代表会在两次运行间漂移。
  排序改为 `(-ts, item_guid)`，同样的数据无论到达顺序，池子顺序唯一。
- **同窗重跑时片内代表被顶掉**，`state["current"]` 与实际内容对不上。改为接受更新并同步回写
  `current`，`window`/`bucket` 不推进（保证的是「游标不推进」，不等于「内容绝对不变」）。

### 变更

- **删除 `ROTATE_MAX_LAG`**。这个参数**从未生效过**：第一级「最近 N 片里取最新的未播片」的
  候选集是第二级「全池取最新的未播片」的子集，两处又都取最大片号，所以两级给出的永远是同一条。
  穷举 2186 组池子状态 × 20 万组随机状态，`0/1/6/24/1000` 各档结果**逐项相同**。
  留着一个假旋钮比没有更糟——README 写着「调大 = 旧条目也能轮到」，实际调大什么都不变。
- `ROTATE_STATE_VERSION` 2 → 3：删 `queue`（不再需要快照），新增 `lag`（= `window - bucket`，
  排障第一眼看的数字）。旧状态会自动丢弃。
- `_pick_bucket()` 从三级台阶缩成单条规则，`w` 形参一并去掉。
- 新增 `tools/sim_rotate.py`：按线上节律（白天 5 分钟一窗、夜间一小时一窗）跑 24 小时，
  量新鲜度 / 上屏名次 / `lag` 分布 / 覆盖率。它的建模修了三处才可信：到达数改泊松
  （`int(pending)` 累加会退化成「每窗恒 2 条、永不为 0」）、发布延迟摊满整窗
  （写 `0~120` 秒时 120 < 300 片长，每窗必然恰好一个新片，「补播」路径 24 小时一次都走不到）、
  夜间积压摊满整小时。
- 设备版覆盖率与池子上限的关系写进 README：产出约 2.35 条/窗、消费 1 条/窗，
  覆盖率上限就是 43%，实测 33~35%。这不是遗漏 bug，是墨水屏 5 分钟一条的物理上限。

### 已知代价（实测，不是估计）

- **旧条目基本轮不到**：白天一直有新内容，几小时前的条目会被 `ITEM_LIMIT` 挤掉而不上屏。
  这是「最新优先」的必然结果，用户已确认接受（另一条路「按比例插播旧内容」没有采用）。
- **尾部来自「零产出的窗」**：约 9% 的窗本窗没有任何新内容（泊松到达会成簇出现空窗），
  这时屏幕要换内容只能从池子存货里挑，中位滞后约 2 小时、最差 4~6 小时（`lag` 最到 63 片）。
  这**不是夜间节律导致的** —— 夜间反而是全天最健康的一段（最大 13 分），因为夜间一小时才构建一次、
  每次抓回一小时的产出，「最新的未播片」总是刚发布的。想压住尾部只能让空窗保持不动，
  但那样「屏幕停住」和「设备坏了」就分不出来了。
- **极端低产能不封顶**：全源停更 2 小时 → `lag` 36 片；产出 0.4 条/窗时实测最差 22.9 小时。
  刻意不设年龄上限——墨水屏不显示时间戳，「内容停住」和「设备坏了」在用户眼里是同一件事。

## [1.1.1] - 2026-10-29

线上首屏回归时发现的两处排版切点问题（1.1.0 发布后 10 分钟内修掉）。

### 修复

- **数字被切成两行**：`…外泄信息超3` + `亿条…`。切点落在数字与量词之间时，
  第一行以孤零零的数字收尾，正文从量词开始，读起来是残句。
- **单位与数字被拆散**：`…亏损2.59亿` + `元，累计…`。同样的毛病，只是切点在单位前一位。

两处都改成「退到这个数量之前再断」：宁可少显示三四个字，换整句通顺；
数量正好在行首、或退得不足行宽一半时仍按原规则硬切。减号也跟着数字走，不留在行尾。

### 变更

- 行为基线更新：`tests/golden.txt` 增加上述两条样本，`split_title` 输出随之变化。
- 版本号语义补一句：修订号除「排版切点类修复」外，输出应逐字节不变。

## [1.1.0] - 2026-10-29

全量代码审查后的修复与优化。设备版（`feed-wrap.xml`）输出**逐字节未变**，
行为基线 diff 只涉及完整版新增的 `<description>` 与 `<generator>`，其余 300+ 行完全一致。

### 修复

- **完整版从不输出正文**：`build_rss()` 在非设备版分支把正文写死成空串，
  `docs/feed.xml` 里 120 条只有标题，原 feed 的导语一段都读不到。
  现在完整版输出清洗后的摘要，单独受 `FEED_BODY_MAX`（默认 300 全角）约束。
  副带修好一处隐性损失：`load_prev_items()` 的回退条目原本 summary 恒为空。
- **deflate 压缩的源整源失败**：`http_get()` 声明 `Accept-Encoding: gzip, deflate`，
  却只解 gzip。源站回 `Content-Encoding: deflate` 时，压缩流被当正文解码，
  解析必然失败并走回退。现在两种算法都解（含裸 deflate 兜底）。
- **忽略响应头声明的字符集**：`decode_body()` 只看 UTF-8 / GB18030。
  声明为 Big5 / Shift-JIS 的源会**静默解成乱码**（解码不报错，只是字全错）。
  现在先信 `Content-Type` 的 charset，认不出再按中文站两个常见编码兜底。
- **同窗内可能换条**：`_hold_current()` 先按 guid 找当前条目，而片内代表会被后到的条目顶掉，
  找不到就判「该条离场」。改为片号优先、guid 兜底。
- **重试阻塞整轮**：重试在主线程的 `as_completed` 循环里 `sleep`，几个源超时就白等几秒，
  期间已完成的结果全都压着不处理。重试下沉到 worker，改为各等各的。
- **`rss.yml` 参数与代码脱节**：`SCREEN_BODY_MAX` 还写着 `60`（代码默认 90），
  且漏了 `PER_SOURCE` / `ITEM_LIMIT`；触发路径漏 `render.py` —— 改排版不会重建、改源时又用另一套参数。
- **`shown` 里会留死标记**：清单外的旧片号一直留在 `shown` 里，进度显示虚高。现在取交集。

### 优化

- **解析少扫几遍全树**：`parse_items()` 原先每个字段各遍历一次子树（title / link / 时间 / 摘要共 4 遍），
  500 条的大源就是两千次节点遍历。改为一次 `_index()` 建表，字段按优先级取值。
- **`feeds.txt` 的无效行不再静默丢弃**：地址写错（缺 `http`、被引号包住、贴成 Markdown 链接）
  以前毫无提示，现在启动时打印「第几行看起来不是地址」。
- **行为基线接入 CI**：`tools/golden_snapshot.py` 以前只能人工在重构前后各跑一次比对，
  谁都可以忘。现在基线入库为 `tests/golden.txt`，由 `tests/test_golden.py` 逐字节比对，
  门禁也会检查基线有没有被手改。

### 新增

- `version.py`：版本号唯一真源。RSS 的 `<generator>`、`build_feed.py --version`、
  两个页面页脚都取这里；门禁校验页面是否跟上了版本号。
- `CHANGELOG.md`：本文件。
- 单测从 24 例增至 39 例：版本语义、完整版正文、解码顺序、feeds.txt 容错、Atom 解析、行为基线。

### 移除

- `write_feed()` 里的死分支 `[pick] if pick else items`（`pick` 已保证非空，`else` 永不执行；
  真执行的话会把 120 条塞进设备版）。
- `_first_text()`（被 `_index()` 取代）、`_sanitize()` 里永不成立的 `&#` 判断。
- `.gitignore` 里旧方案的残留项 `state.json`、`/feed.xml`。
- 工作目录里的 `__pycache__/`。

### 文档

- README / OPERATIONS 里与代码不符的数字全部校准：完整版条目上限 60 → 120、
  一屏可读字数 13+60 → 13+90、一轮时长 3.6 → 4.2 小时、用例数 22 → 39。
- OPERATIONS 排障第 15 节的字段说明改正：`shown` 存的是**时间片号**不是 guid，
  `pool` 是**片数**、`items` 才是条数。
- 明确夜间节奏的口径：`ROTATE_SECONDS` 夜间仍为 300（它定的是时间片粒度，
  夜间每小时只跑一轮，自然一小时换一条）。

## [1.0.0] - 2026-10-29

首个可用版本（此前为同日内快速迭代，行为已定型，此处合并记录）：

- 43 个中文 RSS 源，每源取最新 3 条，跨源两道去重（链接去参、标题去标点后 ≥8 字），上限 120 条。
- 完整版 `docs/feed.xml` 给标准阅读器；设备版 `docs/feed-wrap.xml` 给 Dot (Quote/0) 墨水屏，
  每版只发一条：标题截到 13 全角、余文与摘要进 `<description>`、字段极简。
- 轮播按「时间片」排队 + 清单快照：一窗一片、播完才回头，`w = now // 300` 做幂等键，
  同窗重跑不跳条。
- GitHub Actions 每小时触发、作业内连跑 13 轮（cron 会丢档，只能小时级 + 自循环），
  artifact 部署绕过 Pages 10 次/小时限制；夜间降为每小时一轮。
- 代码审查机制：`CODE_REVIEW.md` 五轴四级标准 + `tools/review_check.py` 可执行门禁 +
  `tools/check_secrets.py` 凭据扫描 + 单测 + 生成物漂移检查。
