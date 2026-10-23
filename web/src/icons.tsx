const P = {
  overview: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z",
  forecast: "M3 17l5-6 4 3 8-9M3 21h18",
  transactions: "M7 4v16M7 20l-3-3M7 20l3-3M17 20V4M17 4l-3 3M17 4l3 3",
  obligations: "M6 3h9l4 4v14H6V3Zm8 0v5h5M9 13h7M9 17h7",
  scenarios: "M5 4v16M5 8h8M5 16h12M13 5v6M17 13v6",
  planfact: "M4 20V10m5 10V4m5 16v-7m5 7V8",
  assistant: "M4 5h16v11H9l-5 4V5Zm5 5h6",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7-3 2-1-2-4-2 1a7 7 0 0 0-2-1l-.5-2h-5L9 7a7 7 0 0 0-2 1L5 7l-2 4 2 1a7 7 0 0 0 0 2l-2 1 2 4 2-1a7 7 0 0 0 2 1l.5 2h5l.5-2a7 7 0 0 0 2-1l2 1 2-4-2-1a7 7 0 0 0 0-2Z",
  bell: "M6 17V11a6 6 0 1 1 12 0v6l2 2H4l2-2Zm4 3a2 2 0 0 0 4 0",
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6 6l12 12M18 6 6 18",
  upload: "M12 16V4m0 0L8 8m4-4 4 4M4 20h16",
  down: "M6 9l6 6 6-6",
  check: "M5 12l5 5 9-10",
  alert: "M12 4 3 20h18L12 4Zm0 6v5m0 3v.01",
  send: "M4 12 20 4l-6 16-3-7-7-1Z",
  plus: "M12 5v14M5 12h14",
  trash: "M5 7h14M10 7V4h4v3m-6 0 1 13h6l1-13",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Zm10 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z",
  eyeOff: "M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4.2M6.6 6.6A17 17 0 0 0 2 12s3.5 7 10 7c1.9 0 3.5-.5 5-1.3M9.9 9.9a3 3 0 0 0 4.2 4.2",
  logout: "M10 5H5v14h5M15 8l4 4-4 4M9 12h10",
} as const;

export type IconName = keyof typeof P;

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={P[name]} />
    </svg>
  );
}
