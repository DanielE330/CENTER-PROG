//! Masking before external providers: identifiers are removed, counterparty
//! names become "Контрагент A/B/…" and are restored in the answer.

use std::{collections::HashMap, sync::OnceLock};

use regex::Regex;

struct Patterns {
    org: Regex,
    inn_or_account: Regex,
    email: Regex,
    phone: Regex,
}

fn patterns() -> &'static Patterns {
    static P: OnceLock<Patterns> = OnceLock::new();
    P.get_or_init(|| {
        #[expect(clippy::expect_used, reason = "constant patterns")]
        let re = |s: &str| Regex::new(s).expect("valid regex");
        Patterns {
            org: re(r#"(?:ООО|АО|ПАО|ЗАО|ОАО|ИП)\s*[«"][^»"]{1,80}[»"]|ИП\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ]\.?\s*){0,2}"#),
            inn_or_account: re(r"\b\d{10}(?:\d{2})?\b|\b\d{20}\b"),
            email: re(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
            phone: re(r"(?:\+7|\b8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b"),
        }
    })
}

#[derive(Default)]
pub struct Masker {
    forward: HashMap<String, String>,
    pub replaced: usize,
}

fn alias(n: usize) -> String {
    const LETTERS: &[char] = &[
        'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'K', 'L', 'M', 'N', 'P', 'R', 'S', 'T',
    ];
    let letter = LETTERS[n % LETTERS.len()];
    if n < LETTERS.len() {
        format!("Контрагент {letter}")
    } else {
        format!("Контрагент {letter}{}", n / LETTERS.len())
    }
}

impl Masker {
    pub fn mask(&mut self, text: &str) -> String {
        let p = patterns();
        let mut out = p
            .org
            .replace_all(text, |c: &regex::Captures<'_>| {
                let original = c[0].to_owned();
                let next = self.forward.len();
                self.replaced += 1;
                self.forward.entry(original).or_insert_with(|| alias(next)).clone()
            })
            .into_owned();
        for (re, label) in [
            (&p.inn_or_account, "[номер скрыт]"),
            (&p.email, "[email скрыт]"),
            (&p.phone, "[телефон скрыт]"),
        ] {
            let before = out.clone();
            out = re.replace_all(&before, label).into_owned();
            if out != before {
                self.replaced += 1;
            }
        }
        out
    }

    /// Puts real names back into the model's answer; identifiers stay hidden.
    pub fn unmask(&self, text: &str) -> String {
        let mut out = text.to_owned();
        // Longer aliases first, so "Контрагент A1" is not broken by "Контрагент A".
        let mut pairs: Vec<_> = self.forward.iter().collect();
        pairs.sort_by_key(|(_, a)| std::cmp::Reverse(a.len()));
        for (original, alias) in pairs {
            out = out.replace(alias.as_str(), original);
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_are_aliased_and_restored() {
        let mut m = Masker::default();
        let masked = m.mask("Оплата от ООО «Элеватор-242» ожидается, ООО «Элеватор-242» платит поздно");
        assert!(!masked.contains("Элеватор"));
        assert_eq!(masked.matches("Контрагент A").count(), 2);
        assert!(m.unmask("Позвоните Контрагент A").contains("ООО «Элеватор-242»"));
    }

    #[test]
    fn inn_account_email_phone_are_removed() {
        let mut m = Masker::default();
        let masked = m.mask("ИНН 0048401219, счёт 40702810000000000001, a@b.ru, +7 (918) 123-45-67");
        for secret in ["0048401219", "40702810000000000001", "a@b.ru", "123-45-67"] {
            assert!(!masked.contains(secret), "{secret} leaked: {masked}");
        }
    }
}
