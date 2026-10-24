import sys

import pandas as pd

d = sys.argv[1]
ops = pd.read_csv(
    f"{d}/all_operations.csv",
    sep=";",
    encoding="utf-8-sig",
    dtype={"counterparty_inn": str},
    parse_dates=["operation_date"],
)
co = pd.read_csv(f"{d}/companies.csv", sep=";", encoding="utf-8-sig", dtype={"company_inn": str})
print(ops.shape, ops.dtypes.to_dict())
print(co.legal_form.value_counts().to_dict())
print(co.industry.value_counts().to_dict())
hist = (co.last_operation_date.astype("datetime64[ns]") - co.first_operation_date.astype("datetime64[ns]")).dt.days
print("history days:", hist.describe().round().to_dict(), "lt90:", (hist < 90).sum())
ops["signed"] = ops.amount.where(ops.direction == "Поступление", -ops.amount)
print(
    "empty purpose:",
    ops.payment_purpose.isna().mean().round(4),
    "empty inn:",
    ops.counterparty_inn.isna().mean().round(4),
)
dup = ops.duplicated(
    subset=["company_id", "operation_date", "amount", "direction", "counterparty_inn", "payment_purpose"]
).sum()
print("dups:", dup)
day = (
    ops.groupby(["company_id", "operation_date"])
    .agg(flow=("signed", "sum"), bal=("balance_eod", "first"), nbal=("balance_eod", "nunique"))
    .reset_index()
)
print("days with >1 balance value:", (day.nbal > 1).sum())
day["prev"] = day.groupby("company_id").bal.shift()
day["diff"] = (day.prev + day.flow - day.bal).round(2)
br = day[day.prev.notna() & (day["diff"].abs() > 0.01)]
print("balance breaks:", len(br), "companies with breaks:", br.company_id.nunique())
gap = day.groupby("company_id").operation_date.diff().dt.days
print("max calendar gap between op days:", gap.describe().round(1).to_dict())
neg = day[day.bal < 0]
print("neg-balance days:", len(neg), "companies:", neg.company_id.nunique())
p = ops.payment_purpose.fillna("")
pats = {
    "УПД/счет с датой": r"(УПД|сч[её]т[ау]?|счет-фактур|договор[ау]?).{0,20}№\s*\S+\s+от\s+\d{2}\.\d{2}\.\d{4}",
    "ЕНП": r"ЕНП|[Ее]дин\w* налог",
    "зарплата": r"заработн|зарплат|аванс",
    "эквайринг": r"эквайр|POS|терминал",
    "собств средства": r"собственн\w* средств",
    "кредит": r"кредит",
    "лизинг": r"лизинг",
    "аренда": r"аренд",
    "возврат": r"возврат|ошибочн",
}
for k, v in pats.items():
    print(k, round(p.str.contains(v, regex=True).mean(), 4))
print(ops.counterparty_name.str.replace(r"[-–]\d+.*", "", regex=True).value_counts().head(40).to_string())
