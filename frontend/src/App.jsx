import MisoLandingPage from "./fake-landingpage/MisoLandingPage.jsx";
import MisoRamen from "./ramen/MisoRamen.jsx";
import "./App.css";

// Composition root: static backdrop page + the Ramen widget on top.
export default function App() {
  return (
    <>
      <MisoLandingPage />
      <MisoRamen />
    </>
  );
}
