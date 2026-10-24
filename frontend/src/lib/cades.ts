// Обёртка над КриптоПро ЭЦП Browser plug-in (cadesplugin_api.js, асинхронный режим).
/* eslint-disable @typescript-eslint/no-explicit-any */
declare global {
  interface Window { cadesplugin?: any }
}

export type CertInfo = { index: number; name: string; issuer: string; validTo: string; thumbprint: string };

const CAPICOM_CURRENT_USER_STORE = 2;
const CAPICOM_MY_STORE = "My";
const CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED = 2;
const CADESCOM_CADES_BES = 1;
const CADESCOM_BASE64_TO_BINARY = 1;

let loading: Promise<void> | null = null;

// window.cadesplugin сам является thenable: его нельзя передавать в resolve() (промис «раскроется» в undefined)
export function loadPlugin(): Promise<void> {
  if (loading) return loading;
  loading = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "/cadesplugin_api.js";
    s.onerror = () => { loading = null; reject(new Error("Не найден cadesplugin_api.js (КриптоПро ЭЦП Browser plug-in)")); };
    s.onload = () => {
      const cp = window.cadesplugin;
      if (!cp) { loading = null; return reject(new Error("Плагин КриптоПро не инициализировался")); }
      const timer = setTimeout(() => { loading = null; reject(new Error("Плагин КриптоПро не отвечает: установите расширение для браузера и КриптоПро CSP")); }, 8000);
      Promise.resolve(cp).then(() => { clearTimeout(timer); resolve(); }, (e: unknown) => { clearTimeout(timer); loading = null; reject(new Error(String(e))); });
    };
    document.head.appendChild(s);
  });
  return loading;
}

const cn = (dn: string) => /(?:^|,\s*)CN=([^,]+)/.exec(dn)?.[1] ?? dn;

export async function listCertificates(): Promise<CertInfo[]> {
  await loadPlugin();
  const cp = window.cadesplugin;
  const store = await cp.CreateObjectAsync("CAdESCOM.Store");
  await store.Open(CAPICOM_CURRENT_USER_STORE, CAPICOM_MY_STORE, CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED);
  try {
    const certs = await store.Certificates;
    const n: number = await certs.Count;
    const out: CertInfo[] = [];
    const now = Date.now();
    for (let i = 1; i <= n; i++) {
      const c = await certs.Item(i);
      const validTo = new Date(await c.ValidToDate);
      if (validTo.getTime() < now || !(await c.HasPrivateKey())) continue;
      out.push({ index: i, name: cn(await c.SubjectName), issuer: cn(await c.IssuerName), validTo: validTo.toLocaleDateString("ru-RU"), thumbprint: await c.Thumbprint });
    }
    return out;
  } finally {
    await store.Close();
  }
}

/** Откреплённая подпись CAdES-BES (base64, DER) для текста challenge. */
export async function signDetached(thumbprint: string, text: string): Promise<string> {
  await loadPlugin();
  const cp = window.cadesplugin;
  const store = await cp.CreateObjectAsync("CAdESCOM.Store");
  await store.Open(CAPICOM_CURRENT_USER_STORE, CAPICOM_MY_STORE, CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED);
  try {
    const found = await (await store.Certificates).Find(0 /* по отпечатку */, thumbprint);
    if ((await found.Count) < 1) throw new Error("Сертификат не найден в хранилище");
    const cert = await found.Item(1);
    const signer = await cp.CreateObjectAsync("CAdESCOM.CPSigner");
    await signer.propset_Certificate(cert);
    await signer.propset_CheckCertificate(true);
    const sd = await cp.CreateObjectAsync("CAdESCOM.CadesSignedData");
    await sd.propset_ContentEncoding(CADESCOM_BASE64_TO_BINARY);
    await sd.propset_Content(btoa(text)); // challenge только ASCII
    return await sd.SignCades(signer, CADESCOM_CADES_BES, true);
  } finally {
    await store.Close();
  }
}
