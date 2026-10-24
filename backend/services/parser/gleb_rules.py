"""Правила категорий из проекта gleb (swimshadow): идея и правила: проект gleb (swimshadow).

Исходник: gleb/rules/categories.yaml. Перенесено 1:1 по regex (YAML-парсер не тянем в зависимости),
кроме двух поправок под наш датасет (см. комментарии «ПОПРАВКА»). Правила проверяются сверху вниз,
побеждает первое; `purpose`/`name` — regex без учёта регистра, направление in/out.
Подкатегории gleb сворачиваются в наши 13 категорий через SUB_TO_CATEGORY.
"""
import re

# (подкатегория gleb, regex по назначению, regex по имени контрагента, направление in/out/None)
GLEB_RULES: list[tuple[str, str | None, str | None, str | None]] = [
    ('acquiring', r'эквайр|мерчант', None, None),
    ('sbp', r'\bсбп\b|qr-код', None, None),
    ('cash_deposit', r'взнос наличных|инкассир|торговая выручка', None, None),
    ('marketplace_payout', r'договору оферты.*за период|маркетплейс|wildberries|ozon', None, 'in'),
    ('bank_fee', r'комиссия за|плата за обслуживание|ведение счета', None, 'out'),
    ('enp', r'\bенп\b|единый налог|единого налог', None, None),
    ('enp', None, r'казначейство|фнс', 'out'),
    ('salary', r'заработн|зарплат', None, None),
    ('contractors_gph', r'договору гпх|по договору гпх', None, None),
    ('own_transfer_in', r'собственных средств с|собственных средств на расчетный счет', None, 'in'),
    ('founder_loan_in', r'предоставление .*займа|заем учредител|заём учредител|взнос в уставный капитал', None, 'in'),
    ('own_transfer_in', r'пополнение счета собственными средствами', None, 'in'),
    ('founder_loan_return', r'возврат .*займа учредител', None, 'out'),
    ('own_transfer_out', r'перевод собственных средств на расчетный счет|собственных средств на счет в', None, 'out'),
    ('owner_withdrawal', r'личн(ый|ую) (счет|карту)|дивиденд|собственных средств ип на счет физ', None, 'out'),
    ('loan_received', r'выдача кредита|предоставление кредита', None, 'in'),
    ('loan_payment', r'погашение .*кредит|проценты по кредит|кредитному договору|процентов за пользование овердрафт|процентов по договору кредитн', None, None),
    ('leasing', r'лизинг', None, None),
    ('rent', r'аренд', None, None),
    ('utilities', r'электроэнерг|водоснабж|водоотвед|теплоснаб|\bтко\b|вывоз отходов|связи|интернет', None, None),
    ('utilities', None, r'энергосбыт|водоканал|телеком|оператор тко', None),
    ('advertising', r'реклам', None, None),
    ('software', r'лицензи|подписк|использования по|программн', None, 'out'),  # ПОПРАВКА: у gleb без направления, из-за чего «разработка программного обеспечения» (поступление) уходила в расходы
    ('fuel_transport', r'топлив|бензин|дизел|провоз|платон', None, None),
    ('refund_in', r'возврат', None, 'in'),
    ('refund_out', r'возврат', None, 'out'),
    ('services', r'бухгалтерск|уборк|клининг|охран|услуг', None, 'out'),
    ('other_in', r'страхового возмещения|субсиди|грант', None, 'in'),
    ('other_out', r'исполнительному листу|штраф|пени', None, 'out'),
    ('customer_payment', r'оплата|предоплата|расчет по договору|по счету|упд|агентскому договору|авансовый платеж|аванс по договору', None, 'in'),
    ('supplier_payment', r'оплата|предоплата|по счету|упд|поставк|подряд|авансовый платеж', None, 'out'),
]

SUB_TO_CATEGORY: dict[str, str] = {
    "acquiring": "revenue", "sbp": "revenue", "cash_deposit": "revenue",
    "marketplace_payout": "revenue", "customer_payment": "revenue",
    "supplier_payment": "suppliers", "services": "suppliers", "fuel_transport": "suppliers",
    "advertising": "marketing",
    "software": "utilities",  # наш utilities включает подписки/хостинг/облако
    "salary": "payroll", "contractors_gph": "payroll",
    "enp": "taxes",
    "rent": "rent", "utilities": "utilities",
    "loan_payment": "loans", "leasing": "loans", "loan_received": "loans",
    "own_transfer_in": "transfers", "own_transfer_out": "transfers", "founder_loan_in": "transfers",
    "founder_loan_return": "transfers", "owner_withdrawal": "transfers",
    "refund_in": "refunds", "refund_out": "refunds",
    "bank_fee": "bank_fees",
    "other_in": "other_income", "other_out": "other_expense",
}

# собственные деньги, пришедшие на счёт: кандидаты в «спасательные поступления»
RESCUE_SUBS = frozenset({"own_transfer_in", "founder_loan_in"})

_COMPILED = [
    (sub, re.compile(p, re.I) if p else None, re.compile(n, re.I) if n else None, d)
    for sub, p, n, d in GLEB_RULES
]


def match_sub(purpose: str, name: str, direction: str) -> str | None:
    """Подкатегория gleb по назначению и/или имени контрагента или None."""
    for sub, prx, nrx, d in _COMPILED:
        if d and d != direction:
            continue
        if prx is None and nrx is None:
            continue
        if prx is not None and not prx.search(purpose or ""):
            continue
        if nrx is not None and not nrx.search(name or ""):
            continue
        return sub
    return None
