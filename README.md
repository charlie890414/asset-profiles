# wealthfolio-profile

以持有 ETF 與境內基金為起點的定期分類資料更新專案，參照 [wealthfolio/asset-profiles](https://github.com/wealthfolio/asset-profiles) 的 v1 JSON 格式。更新預設產生逐檔審核草稿，不直接修改 Wealthfolio。

## 目前可用範圍

首批設定：0050、0051、006201、006208、009826、SPYM、VT、VWRA、FWRA、VALU，以及持有清單中的 38552137C（安聯四季成長組合基金-P）與 73709234D（群益潛力收益多重資產基金 NB）。代號來源是 2026-09-13 的持有紀錄查詢；部分帳戶回應仍有截斷標記，清單不保證涵蓋所有資產。設定不含帳戶、持有數量、成本或 Wealthfolio 資產 ID。BOXX 刻意排除，不納入定期更新範圍。

已實際驗證的來源：元大 Nuxt 公開持股、富邦資產表、BlackRock CSV/XML look-through、SSGA 持股及產業表、Vanguard Global 分頁 GraphQL、Invesco 官方 holdings／aggregate API，以及官方產品 metadata fallback。現行 10 檔 ETF 與兩檔基金都可產生草稿；最新逐檔結果見 [review/review.md](review/review.md)。

基金使用 `moneydj_fund` adapter 讀取 FundDJ 的日期、基金類別配置與每月前十大持股；73709234D 的 NAV 資產類別以群益官方配置交叉核對。基金完整穿透資料未公開時，profile 保留 `classification_notes`，不把部分表格猜成 GICS 或國家權重。

目前注意事項：

- VALU：使用 Vanguard Global GraphQL 的完整游標分頁（2026-08-31），以 `securityTypes=null` 取得 API 回報的 6,743 筆，並檢查基金名稱、日期與分頁終點。
- FWRA：使用 Invesco 官方 holdings index（2,292 筆，2026-09-14）及同日 sector/country aggregate；`Other` 殘餘保留為明確分類列，不展開猜測。
- BOXX：刻意排除，不納入本專案的 ETF 更新清單；其選擇權／現金策略資料保留在來源研究筆記中。
- Morningstar、justETF、ETF.com、VettaFi、SEC N-PORT 尚未實作；目前不宣稱已支援這些備援來源。
- 不含 Wealthfolio 自動同步 API。要寫入使用者分類，仍須另行取得即時資產／taxonomy IDs、原配置與確認。

## 執行

需要 Python 3.13、Node.js 24。Node 只用於讀取元大序列化 Nuxt 狀態；隔離 context 不提供檔案、網路、process 或 require，且設執行逾時。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python scripts/build.py
.venv/Scripts/python -m unittest discover -s tests -v
```

每檔輸出到 `review/drafts/<symbol>.json`，含舊值、新值、變更欄位、來源 URL、日期、SHA-256、覆蓋率、未配置 bp、分類標準、分母及待審原因。`review/summary.json` 與 `review/review.md` 是該次執行摘要。

元大／富邦台股 ETF 若在 EWT／EEMS 與既有 override 中仍缺個股產業，會依序查詢 Vanguard VT 官方完整持股的 `gicsSectorDescription`、TWSE／TPEx 市場身分、Stock Analysis 公司分類與 TradingView 產業分類。查到交易所公司資料後仍會繼續查分類網站；一個網站失敗也會繼續下一個。Vanguard 僅採用台灣股票、代號相符、日期在 90 天內且無衝突的 GICS。

第三方結果須符合市場、股票代號、相同 ISIN，且 Stock Analysis 公司資料更新日期在 90 天內，再由 TradingView 的明確 sector／industry 對照交叉核對，才以 `resolved_crosswalk` 補入本次草稿。TradingView 未公開分類生效日，保留 `as_of_date: null`，只用原始抓取日期判斷佐證是否過期；不把價格日期當分類日期。這是第三方產業大類對照，metadata 會標示與原生 GICS 的差別。對照表在 `scripts/sector_providers.py`，只涵蓋已明確建立的產業配對；未知配對不猜測。單一候選、過期或身分不足維持 `candidate`，分類不一致維持 `conflict`，沒有資料維持 `unresolved`；均不補入權重。既有 override 與已發布資料不會被自動改寫。

查找結果出現在審核報告的「缺漏產業自動查找」及草稿 `metadata.classification_lookup`，包含各次查找的來源 URL、資料日期、取得證據與錯誤。備援查找失敗不會使已取得的 ETF 持股失效，缺漏比例仍保持未分類。`--offline` 使用同一套查找及原始來源快取；沒有快取的備援來源會記錄查找錯誤。每週 workflow 執行相同的 build 指令，因此部署這些變更後即會自動查找。

```powershell
# 僅更新指定檔案（摘要也只涵蓋這次選擇）
.venv/Scripts/python scripts/build.py --symbols 0050.TW VT VWRA.L
# 以已下載的原始來源重播，不把重播時間冒充取得時間
.venv/Scripts/python scripts/build.py --offline
```

來源失敗時保留 `v1/` 既有資料，並將該檔草稿標成 `source_error`，避免錯誤沿用上一輪候選；若官方只有產品 metadata（例如尚未開放持股表），則標成 `metadata_only`，不再把可預期的「暫無持股資料」誤報成來源錯誤。只有所有 adapter 都失敗才回傳 exit code 1；其餘成功草稿仍保存。完整且信心足夠的候選標成 `ready`，分類不完整但來源正常的標成 `needs_review`。`.cache/` 保留本機原始來源及雜湊，不提交 Git。

## 每週執行與發布

[refresh.yml](.github/workflows/refresh.yml) 設定每週日 06:00 UTC（台灣 14:00）及手動觸發。GitHub 排程可能延遲。推到 GitHub 預設分支且啟用 Actions 後才會執行；目前只建立本地排程檔，未建立遠端儲存庫或啟用任何遠端排程。

每次執行會產生 Actions summary 與保留 30 天的 `etf-review-*` artifact，即使部分失敗也保存證據。工作流程權限為 `contents: read`，不自動 commit、push、建立 PR 或合併。未完成來源會使本次 run 失敗，詳情看 summary。

審核確定的個別草稿後，可明確選擇升版；此指令只寫本地靜態 JSON：

```powershell
.venv/Scripts/python scripts/promote.py 0050.TW
.venv/Scripts/python scripts/validate.py v1
```

`promote` 只接受 `ready` 候選，驗證候選雜湊與原版雜湊、資料日期、鎖定及 schema。不接受被修改的候選、舊於已發布版本的來源、過期來源或完全沒有完整分類維度的檔案。`needs_review` 與 `metadata_only` 只留在草稿，不進 v1。這不是信心分數自動核准。

核准後 ETF 會產生 `v1/etfs/<symbol>.json`，基金會產生 `v1/funds/<symbol>.json`，並同步更新 `v1/index.json` 的 `counts.funds`、symbol kind 與 ISIN 映射。公開 GitHub 儲存庫可使用 `https://cdn.jsdelivr.net/gh/OWNER/REPO@main/v1/index.json`；私人儲存庫不適用公開 CDN，必須自行提供有授權的存取方式。

## 配置語意與保護

- 產業與國家是**股票部位穿透曝險**，不是 ETF 法人本身的產業，也不是整檔 ETF 的 NAV 配置。相容 v1 不含分母欄位，因此發布端必須向消費端說明此約定；完整語意保存在審核 metadata。發行商 aggregate 的 `Other` 會保留在 v1；它不是 GICS 產業或可識別國家。
- 元大按官方四捨五入股票權重作分母；富邦、BlackRock、Vanguard 按股票市值作分母。SSGA 直接使用官方產業表，與持股日期必須一致。
- 台灣本土股票型 ETF 的國家維度是投資市場 Taiwan，不等於公司註冊地；國際來源使用發行商 Location 或 Bloomberg ISO country。不能無條件跨這些口徑比較。
- 只在完整分類分母下以最大餘數法分配 10000 bp。未知產業／國家保留未配置，不將 95% 放大成 100%。不刪掉低於 0.01% 的資料再把其餘項目放大。
- EWT／EEMS 官方 GICS 分類用於台股個股映射；其他台股以官方 TWSE／TPEx 產業類別交叉核對 GICS／第三方資料後，才可放入 `config/taiwan-sector-overrides.json`。這些是 broad-sector crosswalk，不把交易所產業名稱直接宣稱為 GICS；衝突來源保留在 override metadata。缺分類不猜測；ICB 不直接當 GICS。映射來源日另存，超過 90 天標示過期。
- 009826 的 NDIA 尚未做穿透，該部分的國家和產業保持未知，不歸為愛爾蘭／金融。
- 不把期貨名目本金加到資產市值，也不把非股票餘額視為銀行存款。全基金資產類別配置尚待逐項 NAV 對帳。
- `manual_overrides/<symbol>.json` 是保護鎖，檔案存在即禁止升版。這版不自動套用 patch，且無法偵測未匯入此專案的 Wealthfolio 使用者修改。
- 信心分數是可解釋的排序指標，非經驗校準機率；所有變更一律需審核。來源優先序在 `config/etfs.json` 的 `sources` 陣列，錯誤才嘗試下一來源，不混合不同日期的配置來湊滿。基金資料的 `asset_class_weights` 代表基金 NAV 配置；若只有前十大持股，維持此限制而不宣稱完整 look-through。

## 出處與授權

`schema/etf.schema.json`、`schema/fund.schema.json`、`schema/index.schema.json` 沿用 Wealthfolio 上游 MIT 程式碼的欄位風格，保留 [LICENSE-UPSTREAM](LICENSE-UPSTREAM)。其餘管線為本專案實作。沒有複製上游整批資料，也沒有假設發行商資料已取得再散布授權；公開資料前依實際來源條款確認用途，provenance 保留來源。

`config/vanguard-query.json` 依 Vanguard 公開網站的查詢欄位建立，使用公開網站 client identifier `uk2`，不是帳戶憑證。
