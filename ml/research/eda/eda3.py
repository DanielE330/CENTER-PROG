import sys
import pandas as pd
d = sys.argv[1]
ops = pd.read_csv(f"{d}/all_operations.csv", sep=";", encoding="utf-8-sig", dtype={"counterparty_inn": str}, parse_dates=["operation_date"])
p = ops.payment_purpose.fillna("")
inc = ops.direction.eq("Поступление")
inj = inc & p.str.contains(r"собственных средств с|займ|заем|Взнос учредител|пополнение счета учредител|на пополнение оборотных", case=False, regex=True)
print("injection-like inflows:", inj.sum(), "companies:", ops[inj].company_id.nunique())
print(p[inj].str.replace(r"\d+","#",regex=True).value_counts().head(12).to_string())
day = ops.assign(s=ops.amount.where(inc,-ops.amount)).groupby(["company_id","operation_date"]).agg(bal=("balance_eod","first"), flow=("s","sum"))
day["open"] = day.bal - day.flow
injd = ops[inj].groupby(["company_id","operation_date"]).amount.sum()
j = day.join(injd.rename("inj"), how="inner")
j["bal_wo"] = j.bal - j.inj
print("injection days:", len(j), "would be negative without injection:", (j.bal_wo<0).sum())
vat = p.str.extract(r"НДС\s*\(?(\d{2})%")[0].value_counts()
print("VAT rates mentioned:", vat.to_dict())
y = ops[p.str.contains(r"ЕНП|[Ее]дин\w* налог", regex=True)]
print("ENP day-of-month:", y.operation_date.dt.day.value_counts().sort_index().to_dict())
doc = p.str.extract(r"(?:УПД|сч[её]ту?|договору)\s*№\s*\S+\s+от\s+(\d{2}\.\d{2}\.\d{4})")[0]
dd = pd.to_datetime(doc, format="%d.%m.%Y", errors="coerce")
lag = (ops.operation_date - dd).dt.days
m = inc & lag.notna()
print("incoming with doc date:", m.sum(), "lag days:", lag[m].describe().round(1).to_dict())
print("outgoing with doc date:", (~inc & lag.notna()).sum(), "lag:", lag[~inc & lag.notna()].describe().round(1).to_dict())
