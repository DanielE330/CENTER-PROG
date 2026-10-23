import sys, re
import pandas as pd
d = sys.argv[1]
ops = pd.read_csv(f"{d}/all_operations.csv", sep=";", encoding="utf-8-sig", dtype={"counterparty_inn": str}, parse_dates=["operation_date"])
p = ops.payment_purpose.fillna("<EMPTY>")
# normalize purposes into templates
t = p.str.replace(r"\d+([.,]\d+)?", "#", regex=True).str.replace(r"\s+"," ",regex=True).str.slice(0,90)
bank = ops.counterparty_name.eq("ПАО «Банк»")
print("BANK templates:\n", t[bank].value_counts().head(25).to_string())
print("\nOTHER templates:\n", t[~bank].value_counts().head(60).to_string())
print("\nunique templates:", t.nunique())
