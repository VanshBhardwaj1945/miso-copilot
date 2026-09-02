import MisoLandingPage from "./fake-landingpage/MisoLandingPage.jsx";
import MisoCopilot from "./copilot/MisoCopilot.jsx";
import "./App.css";

// Composition root: static backdrop page + the Copilot widget on top.
export default function App() {
  return (
    <>
      <MisoLandingPage />
      <MisoCopilot />
    </>
  );
}
