# 源清单

## 先看能不能读到

**能读**

- gov.cn 检索接口（结构化 JSON，带文号、机关、日期、摘要、原文直链）——主入口
- gov.cn 文章页：`/zhengce/content/YYYYMM/content_*.htm`、`/zhengce/zhengceku/YYYYMM/content_*.htm`、`/yaowen/liebiao/YYYYMM/content_*.htm`（政府工作报告、规划纲要、条例、批复都在这里）
- `https://www.gov.cn/zhengce/` 落地页与 `https://www.gov.cn/zhengce/xxgk/`（历次规划纲要、专项规划归档）
- 发改委：`/xxgk/zcfb/tz/`（通知，正文旁带“相关解读”互链）、`/xxgk/jd/jd/`（解读，带“相关政策”回链）、`/hdjl/yjzq/`（意见征求**仅标题与状态**）、`/hdjl/wsgsjdc/`（公示）
- 工信部：首页 `https://www.miit.gov.cn/`（栏目列表页是 JS，首页不是）＋所有 `/art/YYYY/art_*.html` 单品页（意见征集含全文与附件）
- 财政部：`/zhengwuxinxi/zhengcefabu/`、`/zyyjsgkpt/`（预决算与对地方转移支付）、`/zhuantihuigu/cczqzyzfglbf/`

**读不到**（请用户贴原文，或改用检索接口的等价项）

- gov.cn `/zhengce/zuixin/`、`/zhengce/jiedu/`、`/zhengce/wenjian/`（403）、`/gongbao/currentissue.htm`、`/yaowen/liebiao/` 列表页、政策文件库 UI
- 工信部各栏目列表页（政策文件／政策解读／意见征集／文件公示）
- 发改委意见征求的正文（SPA）
- `flk.npc.gov.cn`、司法部草案征集系统、国新办白皮书的栏目页
- 行政法规的替代路径：用检索接口按文种“国令”找

## 主入口：gov.cn 检索接口

```
https://sousuo.www.gov.cn/search-gov/data?t=<TYPE>&q=<关键词>&p=1&n=10
```

**`t` 只有两个值可用**（2026-11 实测）：

| t | 范围 | 状态 |
|---|---|---|
| `zhengcelibrary_gw` | 国务院文件（国发、国办发、国函、国令……） | 可用 |
| `zhengcelibrary_bm` | 国务院部门文件 | 可用 |
| `zhengce` | 曾当作“政策频道（含中央文件）” | **已失效**：任何查询都返回 `totalCount: 0` |
| `govall`、`govxxgk` | — | 返回 `code 1001` |

**请求要点**

- 中文参数必须 URL 编码（`curl --data-urlencode`，或 `urllib.parse.urlencode`）。不编码会返回非 JSON，看起来像接口坏了。
- `n` 上限 100（`n=200` 返回 302 跳转，不是 JSON）。默认 `pageSize=10`，`totalpage` 给总页数。
- 有效筛选：`searchfield=title`（默认 `quanwen`）、`pubtimeyear=2026`（≡ `pcodeYear=2026`）、`puborg=国务院`（机关全名，需编码）。
- **无效**：`mintime`／`maxtime` 不改变结果；`childtype=国发` 返回 `code 1001`。别在它们上面调参。
- 这是非公开接口：今天能用，官方不承诺，随时可能失效。失效时退回“请用户贴原文”。

**响应结构（按错位置取会静默全空，比报错更难查）**

```
{ "code": 200, "msg": "...", "data": null,
  "searchVO": { "listVO": [...], "totalCount": ..., "pageSize": ..., "totalpage": ...,
                "catMap": ..., "dwMap": ..., "ztflTree": ..., "extendresult": ... },
  "paramsVO": ... }
```

- `listVO` 在 **`searchVO`** 下，不在顶层。
- **无结果时 `searchVO` 是 `null`**，同时 `code` 变成 `1001`、`msg` 是“抱歉，没有找到相关结果”。先判 `searchVO` 再判 `code`，否则崩。
- `catMap`（子键 `gongwen`／`bumenfile`，含计数与重复的 `listVO`）、`dwMap`（常为 `null`）、`ztflTree`（约 22 个顶层节点、两层）是分面，不是正文。
- 曾是虚构字段的 `groupMap`、`facetMap` 不存在——不要按它们写解析。

**`listVO` 每条字段**（实测 30 个，常用的这些）：

`title`（含 `<em>` 高亮标签，用前剥掉）、`url`、`pcode`（**就是文号**）、`puborg`、`pubtimeStr`、`summary`、`childtype`、`wenhao`、`wjlx`、`gname`、`theme`、`source`、`subjectword`、`pubtime`、`shixiao`、`id`。

多部门联合发的文件 `pcode` 可能为空，此时用 `wenhao` 或标题＋日期定位。

**体积**：`n=2` 的一次响应 19,516 B，其中 `ztflTree` 11,139 B、`extendresult` 2,648 B、`catMap` 2,372 B，**`listVO` 只有 2,285 B**。所以：只取 `listVO`，绝不把整个响应贴进上下文或报告。想看总量用 `totalCount`，想收窄用 `searchfield=title`＋`pubtimeyear`。

## 按文号直查（核验一件东西真不真，最快的办法）

```
q=<完整文号>&n=100
```

取回后比较 `pcode` **精确相等**，不要只看 total 有没有命中：

- 真实文号的 total 通常很小（1–50），精确命中一条，带标题与日期 → 可引。
- **虚构文号会返回“近似号”**：`财债〔2026〕14号` 返回 total 3，命中的是 `国发〔2026〕14号`、`财办建〔2026〕14号` 这类编号相近、机关相近的真文件。只看“搜到了”就会把它当成真的。这正是虚构文号长期没被发现的机制。<!-- check_wenhao:ignore -->
- 裸号（如 `〔2025〕6号`）会命中多份不同机关的文件，必须带上机关前缀重查。<!-- check_wenhao:ignore -->

脚本已封好，交付前跑一遍：

```
python skill/scripts/check_wenhao.py 报告.md
```

四种结果：`OK`（核实）/`NO-RECORD`（索引无此号）/`NEAR-MISS`（有近似号、精确未命中——伪造特征）/`AMBIGUOUS`（裸号多份）。有疑问时退出码 1。

**未命中 ≠ 造假**。部委自有站点发布、没进 gov.cn 政策文件库的文件查不到（实测：`工信部政法〔2023〕93号`、`京经信发〔2026〕41号` 均查不到，而其内容是真实存在的）。未命中时回原文核对，并注明“索引未收录”，不要写成“此号不存在”。<!-- check_wenhao:ignore -->

## 附件怎么读

征求意见稿与申报指南的正文常在 PDF 附件里，而**抓取工具读不了 PDF**（返回“不支持的内容类型”）。按顺序：

1. **先取文件直链**：附件通常在 `www.miit.gov.cn/cms_files/.../*.pdf` 这类路径上。注意工信部页面给的 `jyhwzhq.miit.gov.cn` 直链会 403，**把主机换成 `www.miit.gov.cn`、路径照抄就是 200**——这一步救回过整份征求意见稿。镜像站与公共代理常 403/5xx，别在上面耗时间。
2. **用 `skill/scripts/read_pdf.py` 读**：
   ```
   python skill/scripts/read_pdf.py 附件.pdf                 # 全文
   python skill/scripts/read_pdf.py 附件.pdf --pages 3,5-7    # 只取证据所在页
   python skill/scripts/read_pdf.py 附件.pdf --json           # 带页码与后端信息
   ```
   后端自动依次试 pypdf → pdfminer.six → 内置纯标准库实现，不需要装任何东西也能读带 ToUnicode CMap 的中文 PDF。退出码 0 有字、1 无字、2 出错。
3. **读出来是空的**：多半是扫描件（图片页，无文字层），内置后端与 pypdf 都取不到。此时不要猜内容——请用户贴正文或截图，并把缺口写进「证据不足」。
4. **退回**：以上都不通时请用户贴正文。用二手转述填这个洞是不行的。

## 三部委看什么

- **发改委**——投向与项目：`/xxgk/zcfb/tz/` 里找“中央预算内投资专项管理办法”“下达投资计划”“重大项目”；产业与专项规划在 `/xxgk/zcfb/ghwb/`（栏目是否可读需现场确认）。
- **工信部**——制造业、数字经济、产业升级：政策文件与解读走单品页；意见征集是**全文可读**的，重点看“起草说明＋附件”。
- **财政部**——钱从哪出：政策发布页；专项资金及其管理办法；预决算公开平台的“中央对地方转移支付”；专项债的额度与项目清单看财政部及省财政厅公告（用检索接口 `puborg=财政部` ＋ 关键词“专项债”收敛，不要凭记忆写文件名）。

三者合看：发改委给方向与项目，工信部给产业落点，财政部给力度与资金形态。

## 其他国家级入口

- **政府工作报告**：检索接口**取不到报告本体**。全文搜“政府工作报告”返回的是法规类；`searchfield=title` 只返回 15 条《国务院关于落实〈政府工作报告〉重点工作分工的意见》（国发〔2022〕9号等）；搜“2026年政府工作报告”返回 `code 1001`。要么从 `/yaowen/` 的文章页取（每年 3 月），要么让用户贴原文；**不要把自己编的句子挂到“政府工作报告”名下**。那份“落实分工意见”本身可引，但名字要写对。
- **五年规划纲要与专项规划**：`/zhengce/xxgk/`，检索接口 `zhengcelibrary_gw` 里也大量可见。
- **重要会议通稿**：新华社／人民日报原发，gov.cn 要闻转载；要闻列表页是 JS，用检索接口找具体篇目。
- **征求意见稿**：没有全国统一聚合页，按部委分别采（工信可读全文，发改只有标题）。

## 地方：定位法（不背清单）

按顺序找，每一步用“关键词＋年份”在站内检索：

1. 本级政府门户 → “政策文件／政策解读”栏目，找**实施方案、行动方案、若干措施**
2. 主管部门（工信局／发改委／人社局／农业农村局……）→ 对应国家方向的落地文件
3. 财政局与发改委 → 资金公示、项目公示、专项资金管理办法、专项债项目清单
4. 政府门户 → 意见征集／公示公告（地方版预期窗口）
5. 产业园区与人才政策 → 搜“招商”“人才”“补贴”“申报指南”；**申报指南里的门槛就是资格线**

地方文件含金量看有没有**配套资金、项目清单、申报指南**三样中的任一样。只有转发和口号的地方文件，视同没有落地。

地方门户的政策解读页常是 JS 渲染，抓不到正文时就只引文件本体（标题＋文号＋日期＋直链），并注明“解读页未取到”。
