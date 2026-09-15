# ETF 缺口來源調查（2026-09-15/16）

來源驗證階段以發行商官方頁面與其第一方 API 為主；第三方資料只列為交叉核對，不作自動發布來源。後續已依本筆記完成 adapter、草稿驗證與 `v1/` 發布；沒有寫入 Wealthfolio。

## VALU.L（Vanguard FTSE Global All-Cap UCITS ETF USD Acc）

- [Vanguard UK E161 頁面](https://www.vanguard.co.uk/professional/product/etf/equity/E161/vanguard-ftse-global-all-cap-ucits-etf-usd-acc) 的 portfolio/holdings 目前顯示暫時不可用。
- 同一基金的 [Vanguard Global/DE E161 頁面](https://global.vanguard.com/de-de/investment-products/etf/equities/E161/ftse-global-all-cap-ucits-etf-usd-acc) 可載入 `https://global.vanguard.com/gpx/graphql` 的 `FundsHoldingsQuery`。以 `portIds=["E161"]` 且 `securityTypes=null` 分頁請求，實測回傳 `totalHoldings=6743`，分頁為 1,500、1,500、1,500、1,500、743 筆，資料日期 `2026-08-31`；每筆含 `ticker`、`securityLongDescription`、`gicsSectorDescription`、`bloombergIsoCountry`、`marketValuePercentage`、`marketValueBaseCurrency` 與 `securityType`。頁面顯式的 security type 清單會少回 313 筆，因此 adapter 保留 null 並以總筆數核對完整性。
- 查到的第一頁基金識別為 `Vanguard FTSE Global All-Cap UCITS ETF USD Acc`、ISIN `IE000VAHT5T0`。因此 VALU 已改用 Vanguard Global GraphQL 作為第一來源，請求使用公開 client header `X-Consumer-ID: de7`，不涉及帳戶憑證。
- 不應以 ESG Global All Cap、FTSE All-World 或第三方 VALL 頁面代替 E161；它們不是同一份即時持股證據。

## FWRA.L（Invesco FTSE All-World UCITS ETF USD Acc）

- [Invesco UK 官方產品頁](https://www.invesco.com/uk/en/financial-products/etfs/invesco-ftse-all-world-ucits-etf-acc.html) 會呼叫 Invesco 第一方 JSON API。實測端點如下：

  ```text
  GET https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/IE000716YHJ7/holdings/index?idType=isin&loadType=initial
  ```

  回傳 `effectiveDate=2026-09-14`、`2292` 筆 holdings；欄位含 `name`、`isin`、`cusip`、`weight`，權重總和約 `99.9989%`。

- 同一 API 也提供與持股日期一致的官方聚合配置：

  ```text
  GET https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/IE000716YHJ7/weightedHoldings/fund?idType=isin&breakdown=sector&audienceType=Financial%20Professional&productType=ETF&productCode=ETF&replicationMethod=Physical
  GET https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/IE000716YHJ7/weightedHoldings/fund?idType=isin&breakdown=country&audienceType=Financial%20Professional&productType=ETF&productCode=ETF&replicationMethod=Physical
  ```

  兩個回應均為 `effectiveDate=2026-09-14`；sector 配置包含 Information Technology 30.93%、Financials 17.46% 等，country 配置包含 United States 63.59%、Japan 6.10%、Taiwan 3.25% 等。

- [Invesco 官方 factsheet](https://www.invesco.com/content/dam/invesco/emea/en/product-documents/etf/share-class/factsheet/IE000716YHJ7_factsheet_en.pdf) 可作為 ISIN、物理複製、指數與基金 metadata 證據。FWRA 已由 `invesco` adapter 讀取 holdings 與兩個同日 aggregate endpoint；完整 holdings 的 `Cash and/or Derivatives` 以非股票殘餘排除於股票分母，sector/country 的 `Other` 原樣保留，不展開猜成國家／產業。
- [StockAnalysis](https://stockanalysis.com/quote/lon/FWRA/holdings/)、[Trackinsight](https://www.trackinsight.com/en/fund/FWRA/holdings) 等第三方頁面可做日期與數量交叉檢查，但不應取代 Invesco 第一方 API，也不應在未確認授權與完整性前寫入發布資料。

## BOXX（Alpha Architect 1-3 Month Box ETF）

- [Alpha Architect 官方 BOXX 頁面](https://funds.alphaarchitect.com/boxetf/) 有每日 holdings；頁面顯示的是 SPY box-spread 的正負選擇權部位、FGXXX 與現金／其他，而不是股票持股清單。
- [SEC prospectus filing](https://www.sec.gov/Archives/edgar/data/1592900/000159290025000206/alphaarchitectboxxprosai.htm) 也確認基金網站每日揭露完整持股。
- 來源已足以支援日後的 `options`／`cash` adapter，但現有分類器會拒絕 signed positions，且 v1 只有股票 sector/country 欄位。要保留期權正負號、抵押品、NAV 分母與資產類別配置後，才可另行發布；不可把 BOXX 直接 look-through 成 SPY 的 GICS 或 Information Technology。現況 `metadata_only` 是正確狀態。

## 結論與建議順序

1. FWRA 已使用 Invesco API adapter，保留完整 holdings、同日 aggregate 與殘餘項目。
2. VALU 已使用 Vanguard Global GraphQL；跑完整分頁與日期／identity 驗證後產生 `ready` 草稿，UK metadata 作 fallback。
3. BOXX 已依目前決策排除，不納入本專案的 ETF 更新清單；若日後重新納入，需另開 derivative-aware 資產類別工作，不與股票 GICS 分類混在一起。
