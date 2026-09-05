import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import ChartBlock from "./ChartBlock.jsx";
import MapBlock from "./MapBlock.jsx";

// Answer renderer: GFM markdown (bold, tables, links), LaTeX math via
// KaTeX ($...$ / $$...$$), code blocks, ```chart -> ChartBlock, ```map -> MapBlock.

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
            return <ChartBlock spec={String(kids)} />;
          }
          if (/language-map/.test(className || "")) {
            return <MapBlock spec={String(kids)} />;
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
