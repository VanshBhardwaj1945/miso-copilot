import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import ChartBlock from "./ChartBlock.jsx";

// Answer renderer: GFM markdown (bold, tables, links), LaTeX math via
// KaTeX ($...$ / $$...$$), code blocks, and ```chart blocks -> ChartBlock.

export default function Markdown({ children }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex]}
      components={{
        a: ({ href, children: kids }) => (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {kids}
          </a>
        ),
        code: ({ className, children: kids, ...props }) => {
          if (/language-chart/.test(className || "")) {
            return <ChartBlock spec={String(kids)} />;
          }
          return (
            <code className={className} {...props}>
              {kids}
            </code>
          );
        },
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
