//! Statement formats. Each format turns a file into `ParsedRow`s; adding a
//! format means adding a `StatementParser` implementation.

use anyhow::{Context, Result, anyhow, bail};
use chrono::NaiveDate;
use ci_domain::{Inn, Kopecks, Sensitive};
use serde::Deserialize;

#[derive(Clone, Debug)]
pub struct ParsedRow {
    pub op_date: NaiveDate,
    pub amount: Kopecks,
    pub inn: Option<Inn>,
    pub counterparty_name: Sensitive,
    pub purpose: Sensitive,
    pub balance_eod: Kopecks,
}

pub trait StatementParser {
    fn name(&self) -> &'static str;
    fn detect(&self, head: &str) -> bool;
    fn parse(&self, bytes: &[u8]) -> Result<Vec<ParsedRow>>;
}

pub fn parsers() -> Vec<Box<dyn StatementParser + Send + Sync>> {
    vec![Box::new(OneCExchange), Box::new(DatasetCsv)]
}

/// Picks a parser by the first bytes of the file.
pub fn parse_any(bytes: &[u8]) -> Result<(&'static str, Vec<ParsedRow>)> {
    let head = decode(&bytes[..bytes.len().min(4096)]);
    for parser in parsers() {
        if parser.detect(&head) {
            return Ok((parser.name(), parser.parse(bytes)?));
        }
    }
    bail!("unknown statement format")
}

/// UTF-8 (with or without BOM) or Windows-1251, which 1C still exports by default.
pub fn decode(bytes: &[u8]) -> String {
    let bytes = bytes.strip_prefix(b"\xEF\xBB\xBF").unwrap_or(bytes);
    match std::str::from_utf8(bytes) {
        Ok(s) => s.to_owned(),
        Err(_) => encoding_rs::WINDOWS_1251.decode(bytes).0.into_owned(),
    }
}

/// Semicolon CSV in the dataset format (one account per file).
pub struct DatasetCsv;

#[derive(Deserialize)]
struct CsvRecord {
    operation_date: String,
    amount: String,
    direction: String,
    counterparty_inn: Option<String>,
    counterparty_name: Option<String>,
    payment_purpose: Option<String>,
    balance_eod: String,
}

impl StatementParser for DatasetCsv {
    fn name(&self) -> &'static str {
        "csv"
    }

    fn detect(&self, head: &str) -> bool {
        head.lines()
            .next()
            .is_some_and(|l| l.contains("operation_date") && l.contains(';'))
    }

    fn parse(&self, bytes: &[u8]) -> Result<Vec<ParsedRow>> {
        let text = decode(bytes);
        let mut reader = csv::ReaderBuilder::new().delimiter(b';').from_reader(text.as_bytes());
        let mut rows = Vec::new();
        for (line, record) in reader.deserialize::<CsvRecord>().enumerate() {
            let rec = record.with_context(|| format!("row {}", line + 2))?;
            rows.push(
                row_from_fields(
                    &rec.operation_date,
                    &rec.amount,
                    &rec.direction,
                    rec.counterparty_inn.as_deref(),
                    rec.counterparty_name.unwrap_or_default(),
                    rec.payment_purpose.unwrap_or_default(),
                    &rec.balance_eod,
                )
                .with_context(|| format!("row {}", line + 2))?,
            );
        }
        Ok(rows)
    }
}

pub fn row_from_fields(
    date: &str,
    amount: &str,
    direction: &str,
    inn: Option<&str>,
    name: String,
    purpose: String,
    balance: &str,
) -> Result<ParsedRow> {
    let op_date = NaiveDate::parse_from_str(date.trim(), "%Y-%m-%d").context("operation_date")?;
    let abs = Kopecks::parse(amount)?;
    if abs.0 < 0 {
        bail!("amount must be positive, direction carries the sign");
    }
    let amount = match direction.trim() {
        "Поступление" | "inflow" => abs,
        "Списание" | "outflow" => Kopecks(-abs.0),
        _ => bail!("unknown direction"),
    };
    Ok(ParsedRow {
        op_date,
        amount,
        inn: inn
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .and_then(|s| Inn::parse(s).ok()),
        counterparty_name: Sensitive::new(name.trim()),
        purpose: Sensitive::new(purpose.trim()),
        balance_eod: Kopecks::parse(balance)?,
    })
}

/// 1CClientBankExchange: the text format almost every Russian bank exports.
/// End-of-day balances are rebuilt from the opening balance of the account section.
pub struct OneCExchange;

impl StatementParser for OneCExchange {
    fn name(&self) -> &'static str {
        "1c"
    }

    fn detect(&self, head: &str) -> bool {
        head.trim_start().starts_with("1CClientBankExchange")
    }

    fn parse(&self, bytes: &[u8]) -> Result<Vec<ParsedRow>> {
        let text = decode(bytes);
        let mut account = String::new();
        let mut opening: Option<Kopecks> = None;
        let mut docs: Vec<Vec<(String, String)>> = Vec::new();
        let mut current: Option<Vec<(String, String)>> = None;
        let mut in_account = false;
        for line in text.lines() {
            let line = line.trim_end_matches('\r');
            if line.starts_with("СекцияДокумент") {
                current = Some(Vec::new());
                continue;
            }
            if line == "КонецДокумента" {
                if let Some(doc) = current.take() {
                    docs.push(doc);
                }
                continue;
            }
            if line == "СекцияРасчСчет" {
                in_account = true;
                continue;
            }
            if line == "КонецРасчСчет" {
                in_account = false;
                continue;
            }
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            if let Some(doc) = current.as_mut() {
                doc.push((key.to_owned(), value.to_owned()));
            } else if in_account && key == "НачальныйОстаток" && opening.is_none() {
                opening = Some(Kopecks::parse(value)?);
            } else if key == "РасчСчет" && account.is_empty() {
                account = value.trim().to_owned();
            }
        }
        if account.is_empty() {
            bail!("1C file has no РасчСчет");
        }
        let opening = opening.ok_or_else(|| anyhow!("1C file has no НачальныйОстаток"))?;
        let mut rows: Vec<ParsedRow> = docs.iter().map(|doc| one_c_row(doc, &account)).collect::<Result<_>>()?;
        rows.sort_by_key(|r| r.op_date);
        // Balance at the end of each day = opening + all flows up to that day.
        let mut running = opening.0;
        let mut i = 0;
        while i < rows.len() {
            let day = rows[i].op_date;
            let mut j = i;
            while j < rows.len() && rows[j].op_date == day {
                running += rows[j].amount.0;
                j += 1;
            }
            for row in &mut rows[i..j] {
                row.balance_eod = Kopecks(running);
            }
            i = j;
        }
        Ok(rows)
    }
}

fn field<'a>(doc: &'a [(String, String)], key: &str) -> Option<&'a str> {
    doc.iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.as_str())
        .filter(|v| !v.is_empty())
}

fn one_c_row(doc: &[(String, String)], account: &str) -> Result<ParsedRow> {
    let outgoing = field(doc, "ПлательщикСчет") == Some(account);
    let date_raw = if outgoing {
        field(doc, "ДатаСписано").or_else(|| field(doc, "Дата"))
    } else {
        field(doc, "ДатаПоступило").or_else(|| field(doc, "Дата"))
    }
    .ok_or_else(|| anyhow!("document without date"))?;
    let op_date = NaiveDate::parse_from_str(date_raw, "%d.%m.%Y").context("1C date")?;
    let abs = Kopecks::parse(field(doc, "Сумма").ok_or_else(|| anyhow!("document without amount"))?)?;
    let (inn_key, name_key) = if outgoing {
        ("ПолучательИНН", "Получатель1")
    } else {
        ("ПлательщикИНН", "Плательщик1")
    };
    let name = field(doc, name_key)
        .or_else(|| {
            field(
                doc,
                if outgoing {
                    "Получатель"
                } else {
                    "Плательщик"
                },
            )
        })
        .unwrap_or_default();
    Ok(ParsedRow {
        op_date,
        amount: if outgoing { Kopecks(-abs.0) } else { abs },
        inn: field(doc, inn_key).and_then(|s| Inn::parse(s).ok()),
        counterparty_name: Sensitive::new(name),
        purpose: Sensitive::new(field(doc, "НазначениеПлатежа").unwrap_or_default()),
        balance_eod: Kopecks(0),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const CSV: &str = "\u{feff}company_id;operation_id;operation_date;amount;direction;counterparty_inn;counterparty_name;payment_purpose;balance_eod\n\
C001;C001-1;2024-10-01;990.00;Списание;0068275202;ПАО «Банк»;Комиссия;1244821.98\n\
C001;C001-2;2024-10-02;100.50;Поступление;;Физическое лицо;;1244922.48\n";

    #[test]
    fn dataset_csv_is_detected_and_signed() {
        let (format, rows) = parse_any(CSV.as_bytes()).unwrap();
        assert_eq!(format, "csv");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].amount, Kopecks(-99_000));
        assert_eq!(rows[1].amount, Kopecks(10_050));
        assert!(rows[1].inn.is_none());
    }

    #[test]
    fn one_c_rebuilds_daily_balances() {
        let file = "1CClientBankExchange\nРасчСчет=40702810000000000001\nСекцияРасчСчет\nНачальныйОстаток=1000.00\nКонецРасчСчет\n\
СекцияДокумент=Платежное поручение\nДата=01.10.2024\nСумма=100.00\nПлательщикСчет=40702810000000000001\nПолучательИНН=0068275202\nПолучатель1=ООО Ромашка\nНазначениеПлатежа=Аренда\nКонецДокумента\n\
СекцияДокумент=Платежное поручение\nДата=01.10.2024\nСумма=50.00\nПлательщикСчет=40702810999999999999\nПлательщикИНН=0048401219\nПлательщик1=ООО Клиент\nНазначениеПлатежа=Оплата\nКонецДокумента\nКонецФайла\n";
        let (format, rows) = parse_any(file.as_bytes()).unwrap();
        assert_eq!(format, "1c");
        assert!(rows.iter().all(|r| r.balance_eod == Kopecks(95_000)));
    }

    #[test]
    fn windows_1251_is_decoded() {
        let (bytes, _, _) = encoding_rs::WINDOWS_1251.encode("Поступление");
        assert_eq!(decode(&bytes), "Поступление");
    }
}
