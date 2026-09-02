import "./MisoLandingPage.css";

// Static demo backdrop — no logic, no routing. The Copilot sits on top.
// Abstract look-alike only; never MISO's real logo/assets.

export default function MisoLandingPage() {
  return (
    <div className="miso-page">
      <nav className="miso-nav">
        <div className="miso-logo">
          <div className="miso-logo-mark">
            <span className="miso-logo-sun" />
            <span className="miso-logo-leaf" />
          </div>
          <span className="miso-logo-text">MISO</span>
        </div>

        <div className="miso-nav-links">
          <a href="#about">About MISO</a>
          <a href="#work">Our Work</a>
          <a href="#stakeholders">Stakeholder Engagement</a>
          <a href="#careers">Careers</a>
          <a href="#news">News &amp; Events</a>
        </div>

        <button className="miso-search" aria-label="Search">
          <span />
        </button>
      </nav>

      <main>
        <section className="miso-hero">
          <div className="power-grid" aria-hidden="true">
            <div className="tower tower-one" />
            <div className="tower tower-two" />
            <div className="tower tower-three" />
            <div className="tower tower-four" />
          </div>

          <div className="hero-glow" aria-hidden="true" />

          <div className="miso-hero-content">
            <span className="hero-eyebrow">
              MIDCONTINENT INDEPENDENT SYSTEM OPERATOR
            </span>

            <h1>
              Powering
              <br />
              <span>Reliability.</span>
            </h1>

            <p>
              We ensure the reliable delivery of electricity across the
              Midwest through effective grid management, market operations,
              and planning.
            </p>

            <div className="hero-buttons">
              <button className="primary-button">About MISO</button>
              <button className="secondary-button">Our Work</button>
            </div>
          </div>

          <div className="miso-stats">
            <div className="stat">
              <strong>15</strong>
              <span>States + Manitoba</span>
            </div>
            <div className="stat">
              <strong>200,000+</strong>
              <span>Square Miles</span>
            </div>
            <div className="stat">
              <strong>129,000 MW</strong>
              <span>Peak Demand</span>
            </div>
            <div className="stat">
              <strong>24/7</strong>
              <span>Grid Operations</span>
            </div>
          </div>
        </section>

        <section className="miso-content-section">
          <div className="mission-intro">
            <span className="section-eyebrow">OUR MISSION</span>
            <h2>Reliable electricity for the Midwest.</h2>
            <p>
              MISO operates one of the largest regional transmission
              organizations in the United States, coordinating the flow of
              electricity across the region while maintaining reliability
              and supporting competitive markets.
            </p>
          </div>

          <div className="mission-cards">
            <div className="mission-card">
              <span>01</span>
              <h3>Reliability</h3>
              <p>Keeping the power grid reliable every hour of every day.</p>
            </div>

            <div className="mission-card">
              <span>02</span>
              <h3>Markets</h3>
              <p>
                Operating competitive electricity markets across the MISO
                footprint.
              </p>
            </div>

            <div className="mission-card">
              <span>03</span>
              <h3>Planning</h3>
              <p>Preparing the grid for the changing energy landscape.</p>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
