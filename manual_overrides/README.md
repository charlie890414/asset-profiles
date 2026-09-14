# 手動保護鎖

建立 `<symbol>.json`（例如 `0050.TW.json`），即可阻止該檔草稿被 `promote.py` 升版。建議內容：

```json
{"reason":"人工維護的配置，須逐項審核","owner":"manual"}
```

目前僅作鎖定，不套用 JSON patch。外部 Wealthfolio 的使用者修改不會自動匯入；本專案沒有讀寫使用者分類 API。
