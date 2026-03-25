---
description: "DIC payment channel reconciliation webapp. Invoke when user needs to reconcile DIC backend orders with haipay channel orders, match orders, categorize unmatched orders by transaction type or direction, and export reconciliation results."
---

# DIC Payment Channel Reconciliation

This skill manages and operates the DIC payment channel reconciliation web application.

## Application Features

1. **Data Import**
   - Upload DIC backend orders Excel file
   - Upload haipay channel orders Excel file
   - Support for both xlsx and xls formats

2. **Field Selection**
   - Select matching field for DIC orders (default: "外部订单号")
   - Select matching field for haipay orders (default: "Order No")

3. **Reconciliation Logic**
   - Match DIC backend orders with haipay channel orders based on selected fields
   - Categorize unmatched orders:
     - **渠道有票后台无票**: haipay has the order but DIC backend doesn't (when 变动方向 != 1)
     - **其他类型未匹配**: Other unmatched orders (when 变动方向 == 1)
     - **后台有票渠道无票**: DIC backend has the order but haipay channel doesn't
     - **已匹配**: Successfully matched orders

4. **Results Display**
   - Show reconciliation statistics with metrics
   - Display results in tabs: 已匹配订单, 渠道有票后台无票 (代收), 后台有票渠道无票, 其他类型未匹配
   - Show sample data for each category

5. **Export Functionality**
   - Export individual category results as CSV
   - Export all results as Excel file

## How to Run

```bash
python -m streamlit run dic_reconciliation_app.py
```

The application will start on http://localhost:8501 (port may vary)

## Usage

1. Upload DIC backend orders Excel file
2. Upload haipay channel orders Excel file
3. Select matching fields for both
4. Click "开始自动对账" button
5. View reconciliation results in different tabs
6. Export results as needed

## Key Files

- `dic_reconciliation_app.py`: Main Streamlit application
- Data folder: `C:\Users\w\Desktop\DIC支付渠道对账`

## Troubleshooting

- If port is occupied, the app will try next available port
- Ensure Excel files have correct format and required columns
- Check matching field selection if no matches found