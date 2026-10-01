import type { Metadata } from "next";
import { PT_Serif, Manrope } from "next/font/google";
import "./globals.css";
import { AuthProvider } from "@/lib/auth-context";
import { ThemeProvider } from "@/lib/theme-context";

const ptSerif = PT_Serif({
  variable: "--font-pt-serif",
  subsets: ["latin", "cyrillic"],
  weight: ["400", "700"],
  style: ["normal", "italic"],
});

const manrope = Manrope({
  variable: "--font-manrope",
  subsets: ["latin", "cyrillic"],
  weight: ["400", "500", "600", "700"],
});

export const metadata: Metadata = {
  title: "LegalSearch",
  description: "AI-поиск по нормативным актам и судебной практике",
};

// Ставим сохранённую тему ДО первой отрисовки, чтобы не было мигания светлой темой.
const themeInitScript = `
(function () {
  try {
    var t = localStorage.getItem("legalsearch-theme");
    if (t === "light") document.documentElement.setAttribute("data-theme", "light");
  } catch (e) {}
})();
`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="ru"
      className={`${ptSerif.variable} ${manrope.variable} h-full antialiased`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className="h-full flex flex-col bg-bg text-text">
        <ThemeProvider>
          <AuthProvider>{children}</AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
