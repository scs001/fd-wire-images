import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import zh from "./zh";
import en from "./en";

export const LANGUAGE_KEY = "wire-web-lang";
export const LANGUAGES = ["zh", "en"] as const;
export type Language = (typeof LANGUAGES)[number];

const initial =
  typeof localStorage !== "undefined" &&
  LANGUAGES.includes(localStorage.getItem(LANGUAGE_KEY) as Language)
    ? (localStorage.getItem(LANGUAGE_KEY) as Language)
    : "zh";

i18n.use(initReactI18next).init({
  resources: {
    zh: { translation: zh },
    en: { translation: en },
  },
  lng: initial,
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

export function switchLanguage(lang: Language): void {
  i18n.changeLanguage(lang);
  localStorage.setItem(LANGUAGE_KEY, lang);
  document.documentElement.lang = lang;
}

document.documentElement.lang = initial;

export default i18n;
