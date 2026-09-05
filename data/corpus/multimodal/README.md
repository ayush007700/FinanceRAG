# Multimodal test corpus

Public IRS documents downloaded for FinanceRAG multimodal testing (forms/tables/figures).

| File | Source |
|------|--------|
| `irs_form_6765_rd_credit.pdf` | [IRS Form 6765](https://www.irs.gov/pub/irs-pdf/f6765.pdf) — R&D credit |
| `irs_instructions_6765.pdf` | [Instructions for 6765](https://www.irs.gov/pub/irs-pdf/i6765.pdf) |
| `irs_pub_946_depreciation.pdf` | [Pub 946](https://www.irs.gov/pub/irs-pdf/p946.pdf) — Depreciation |
| `irs_form_3115_accounting_method.pdf` | [Form 3115](https://www.irs.gov/pub/irs-pdf/f3115.pdf) — Accounting method change |
| `sample_cost_seg_schedule.png` | Synthetic demo image (cost segregation table) |

## Index locally

```powershell
finance-rag index data/corpus/multimodal
```

Or via API: `POST /v1/index` with `{"paths":["data/corpus/multimodal"]}`.

Vision captioning uses OpenAI and may take several minutes for large PDFs.
