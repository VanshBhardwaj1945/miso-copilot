import { Component } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import ChartBlock from "./ChartBlock.jsx";
import MapBlock from "./MapBlock.jsx";

// Answer renderer: GFM markdown (bold, tables, links), LaTeX math via
// KaTeX ($...$ / $$...$$), code blocks, ```chart -> ChartBlock, ```map -> MapBlock.

// React unmounts the entire root on an uncaught render error - not the block,
// not the panel, the whole page including the landing page above it, with the
// conversation lost because the messages live in useState. The chart and map
// specs are written by the model and validated nowhere else, so each block
// gets its own boundary and degrades to the raw JSON: the same fallback a
// malformed spec already uses, already styled.
class BlockBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <pre className="miso-md-badchart">
        <code>{this.props.spec}</code>
      </pre>
    );
  }
}

export default function Markdown({ children }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex]}
      components={{
        // Links open in a new tab so the chat stays put.
        a: ({ href, children: kids }) => (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {kids}
          </a>
        ),
        // ```chart blocks become charts; everything else stays a code block.
        code: ({ className, children: kids, ...props }) => {
          if (/language-chart/.test(className || "")) {
            return (
              <BlockBoundary spec={String(kids)}>
                <ChartBlock spec={String(kids)} />
              </BlockBoundary>
            );
          }
          if (/language-map/.test(className || "")) {
            return (
              <BlockBoundary spec={String(kids)}>
                <MapBlock spec={String(kids)} />
              </BlockBoundary>
            );
          }
          return (
            <code className={className} {...props}>
              {kids}
            </code>
          );
        },
        // Wrap tables so wide ones scroll instead of breaking the panel.
        table: ({ children: kids }) => (
          <div className="miso-md-tablewrap">
            <table>{kids}</table>
          </div>
        ),
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
