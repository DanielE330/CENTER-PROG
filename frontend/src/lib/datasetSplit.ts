/** The organisers' dataset can come as one file for all companies (all_operations.csv, first column company_id).
 * The server takes one company's statement, so the file is split here and only the chosen company is uploaded. */
export interface DatasetCompany {
  id: string
  rows: number
  name?: string
  inn?: string
}

export interface SplitDataset {
  fileName: string
  header: string
  companies: DatasetCompany[]
  lines: Map<string, string[]>
}

const SEP = /[;,\t]/

function firstCell(line: string): string {
  const m = SEP.exec(line)
  return (m ? line.slice(0, m.index) : line).replace(/^"|"$/g, '').trim()
}

/** null — the file is an ordinary statement (one company or no company_id column). */
export async function splitByCompany(file: File, names?: Map<string, { name: string; inn: string }>): Promise<SplitDataset | null> {
  if (!/\.(csv|txt)$/i.test(file.name)) return null
  const text = (await file.text()).replace(/^﻿/, '')
  const nl = text.indexOf('\n')
  const header = (nl < 0 ? text : text.slice(0, nl)).replace(/\r$/, '')
  if (firstCell(header).toLowerCase() !== 'company_id') return null
  const lines = new Map<string, string[]>()
  for (const raw of text.slice(nl + 1).split('\n')) {
    const line = raw.replace(/\r$/, '')
    if (!line.trim()) continue
    const id = firstCell(line)
    const arr = lines.get(id)
    if (arr) arr.push(line)
    else lines.set(id, [line])
  }
  if (lines.size < 2) return null
  const companies = [...lines.entries()]
    .map(([id, l]) => ({ id, rows: l.length, name: names?.get(id)?.name, inn: names?.get(id)?.inn }))
    .sort((a, b) => a.id.localeCompare(b.id))
  return { fileName: file.name, header, companies, lines }
}

/** companies.csv: company_id;company_name;company_inn;… */
export async function readCompanies(file: File): Promise<Map<string, { name: string; inn: string }>> {
  const text = (await file.text()).replace(/^﻿/, '')
  const [head, ...rows] = text.split('\n').map((l) => l.replace(/\r$/, ''))
  const cols = head.split(';').map((c) => c.trim().toLowerCase())
  const iId = cols.indexOf('company_id')
  const iName = cols.indexOf('company_name')
  const iInn = cols.indexOf('company_inn')
  const out = new Map<string, { name: string; inn: string }>()
  if (iId < 0) return out
  for (const r of rows) {
    if (!r.trim()) continue
    const f = r.split(';')
    out.set(f[iId], { name: iName >= 0 ? f[iName] ?? '' : '', inn: iInn >= 0 ? f[iInn] ?? '' : '' })
  }
  return out
}

export function isCompaniesFile(file: File) {
  return /companies\.csv$/i.test(file.name)
}

/** One company's statement as a separate CSV file, in the same format. */
export function companyFile(d: SplitDataset, id: string): File {
  const body = [d.header, ...(d.lines.get(id) ?? [])].join('\n') + '\n'
  return new File(['﻿' + body], `${id}.csv`, { type: 'text/csv' })
}

/** One operation for POST /evaluate: signed amount and end-of-day balance in kopecks. */
export interface EvalRow {
  date: string
  amount: number
  counterparty: string
  inn: string
  purpose: string
  balance: number | null
}

function kop(raw: string): number | null {
  const s = raw.trim().replace(/\s/g, '').replace(',', '.')
  if (!/^-?\d+(\.\d{1,2})?$/.test(s)) return null
  const neg = s.startsWith('-')
  const [i, f = ''] = s.replace('-', '').split('.')
  const v = Number(i) * 100 + Number((f + '00').slice(0, 2))
  return neg ? -v : v
}

/** Rows of one company in the dataset format (company_id;operation_id;operation_date;amount;direction;inn;name;purpose;balance_eod). */
export function evalRows(d: SplitDataset, id: string): EvalRow[] {
  const out: EvalRow[] = []
  for (const line of d.lines.get(id) ?? []) {
    const f = line.split(';')
    if (f.length < 9) continue
    const amount = kop(f[3])
    if (amount == null || !/^\d{4}-\d{2}-\d{2}$/.test(f[2])) continue
    out.push({
      date: f[2],
      amount: f[4].trim() === 'Поступление' ? amount : -amount,
      inn: f[5],
      counterparty: f[6],
      // the purpose may contain ';' itself
      purpose: f.slice(7, f.length - 1).join(';'),
      balance: kop(f[f.length - 1]),
    })
  }
  return out
}
