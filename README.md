# 融資維持率與台灣加權指數

這個專案每天更新上市、上櫃與合併融資維持率，並把台灣加權股價指數與 Macromicro old 維持率一起寫入 CSV，供 `index.html` 在 GitHub Pages 上繪製互動圖表。

## 資料檔

- `data/margin_maintenance.csv`
  - 融資維持率、融資市值、融資金額
  - 台灣加權股價指數 OHLC
  - `MacroMicroOldMarginMaintenanceRate`
  - 每日抓取狀態

## 計算口徑

```text
融資市值(仟元) = 融資餘額(張 / 交易單位) * 收盤價
融資維持率 = 融資市值(仟元) / 融資金額(仟元) * 100
```

預設圖表使用 `CombinedMarginMaintenanceRate_ExcludeETF`，也就是上市 + 上櫃、不含 ETF 的綜合融資維持率。

## 資料來源

- TWSE 上市融資融券餘額：<https://www.twse.com.tw/zh/trading/margin/mi-margn.html>
- TPEX 上櫃融資融券餘額：<https://www.tpex.org.tw/zh-tw/mainboard/trading/margin-trading/transactions.html>
- TWSE 發行量加權股價指數歷史資料：<https://www.twse.com.tw/zh/indices/taiex/mi-5min-hist.html>
- Yahoo Finance `^TWII` 參考頁：<https://tw.stock.yahoo.com/quote/%5ETWII>
- Macromicro old：本機 `macromicro-old-maintenance-margin-rate.xlsx` 匯入後寫入 CSV

## 本機更新

```bash
pip install -r requirements.txt
python scripts/update_data.py --days 3
```

指定日期區間：

```bash
python scripts/update_data.py --start-date 2026-08-04 --end-date 2026-08-06
```

## GitHub Actions

`.github/workflows/update-data.yml` 每天 `09:00 UTC` 執行，等於台北時間 `17:00`。流程會抓最近 3 個台股營業日，合併到 CSV；如果 CSV 有變動，就自動 commit 並 push。

`.github/workflows/deploy-pages.yml` 會在 `main` 有更新時部署 GitHub Pages。
