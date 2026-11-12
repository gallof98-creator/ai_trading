import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "QUANT AI TRADER", description: "Multi-asset algorithmic trading and research platform" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
