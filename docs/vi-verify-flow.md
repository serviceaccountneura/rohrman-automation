# Vendor Invoice Verify — End-to-end flow

Rohrman · `PO_INVOICE` path (Miscellaneous / Stock / Sublet).  
**No PO create, no pre-invoice** — verify against Tekion only; attach & complete only when `TEKION_PO_WRITES` is on.

```mermaid
flowchart TD
  A[Upload invoice<br/>PO_INVOICE folder] --> B[OCR read]
  B --> C{PO # + Invoice # + Amount<br/>readable?}
  C -->|No| X1[EXCEPTION<br/>missing fields]
  C -->|Yes| D[Lookup PO in Tekion]

  D --> E{PO found?}
  E -->|No| X2[EXCEPTION · Waiting<br/>PO_NOT_FOUND]
  E -->|Yes| F{Already pre-invoiced?}

  F -->|No| X3[EXCEPTION · Waiting<br/>PRE_INVOICE_MISSING]
  F -->|Yes| G[Find Vendor Invoice<br/>Pre Invoice list]

  G --> H{VI found?}
  H -->|No| X3
  H -->|Yes| I[Header checks — yellow fields]

  I --> I1[PO #]
  I --> I2[Vendor]
  I --> I3[Invoice #]
  I --> I4[Invoice amount]
  I --> I5[Invoice date]

  I1 & I2 & I3 & I4 & I5 --> J{All match?}
  J -->|No| X4[EXCEPTION · PO / Invoice<br/>clear mismatch message]
  J -->|Yes| K{Near-duplicate?<br/>e.g. BLK123 vs 123}

  K -->|Yes| X5[EXCEPTION · Duplicate]
  K -->|No| L[Load JE / GL postings]

  L --> M{Expense GLs present?}
  M -->|No| X6[EXCEPTION · PO<br/>NO_GL]
  M -->|Yes| N[Control checks]

  N --> N1{GL = 2460 Sublet?}
  N1 -->|Yes| N2{Control = RO #?}
  N1 -->|No| N3{Control = PO vendor name?}
  N2 -->|No| X7[EXCEPTION · Journal entry<br/>JE_CONTROL_REQUIRED]
  N3 -->|No| X7
  N2 -->|Yes| N4[Control OK]
  N3 -->|Yes| N5{GL starts with 7?}
  N5 -->|Yes| N6{Description filled?}
  N6 -->|No| X7
  N6 -->|Yes| N4
  N5 -->|No| N4

  N4 --> O{TEKION_PO_WRITES on?}
  O -->|No — test mode| P[PROCESSED<br/>verify only — nothing posted]
  O -->|Yes| Q[Attach PDF + complete VI]
  Q --> R[PROCESSED]

  X1 & X2 & X3 & X4 & X5 & X6 & X7 --> S[Exceptions UI<br/>by category + three-way match]
  P & R --> T[Folder queue · Checks passed]
```

## Header fields verified (Vendor Invoice screen)

- PO Number  
- Vendor Number / Name  
- Invoice Number (full, including letters/suffixes)  
- Invoice Amount (to the cent)  
- Invoice Date  

## Journal Entry — Control rules

- Most expense GLs: **Control = PO vendor name**  
- Sublet GL **2460**: **Control = Repair Order #**  
- GLs starting with **7**: also need a **Description**  

---

Also available as a browser file: [`vi-verify-flow.html`](./vi-verify-flow.html) (open in Chrome/Edge to see the diagram).
