import React from "react";
import { createRoot } from "react-dom/client";
import "./i18n";
import App from "./App";
import { StreamProvider } from "./stream";
import "./style.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <StreamProvider>
      <App />
    </StreamProvider>
  </React.StrictMode>,
);
