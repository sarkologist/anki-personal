// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::borrow::Cow;
use std::sync::LazyLock;

use regex::Captures;
use regex::Regex;

use crate::cloze::expand_clozes_to_reveal_latex;
use crate::media::files::sha1_of_data;
use crate::text::decode_entities;
use crate::text::strip_html;

pub(crate) static LATEX: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?xsi)
            \[latex\](.+?)\[/latex\]     # 1 - standard latex
            |
            \[\$\](.+?)\[/\$\]           # 2 - inline math
            |
            \[\$\$\](.+?)\[/\$\$\]       # 3 - math environment
            |
            <anki-latex\b([^>]*)>(.+?)</anki-latex> # 4/5 - editor-only legacy latex
            ",
    )
    .unwrap()
});
static ANKI_LATEX_DISPLAY_KIND: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?xi)\bdata-latex-kind\s*=\s*(?:"display"|'display'|display(?:\s|/|$))"#).unwrap()
});
static ANKI_LATEX_DATA_ATTR: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?xsi)\bdata-latex\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>/]+))"#).unwrap()
});
static LATEX_NEWLINES: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?xi)
            <br( /)?>
            |
            <div>
        "#,
    )
    .unwrap()
});

pub(crate) fn contains_latex(text: &str) -> bool {
    LATEX.is_match(text)
}

#[derive(Debug, PartialEq, Eq)]
pub struct ExtractedLatex {
    pub fname: String,
    pub latex: String,
}

/// Expand any cloze deletions, then extract LaTeX.
pub(crate) fn extract_latex_expanding_clozes(
    text: &str,
    svg: bool,
) -> (Cow<'_, str>, Vec<ExtractedLatex>) {
    if text.contains("{{c") {
        let expanded = expand_clozes_to_reveal_latex(text);
        let (text, extracts) = extract_latex(&expanded, svg);
        (text.into_owned().into(), extracts)
    } else {
        extract_latex(text, svg)
    }
}

/// Extract LaTeX from the provided text.
/// Expects cloze deletions to already be expanded.
pub fn extract_latex(text: &str, svg: bool) -> (Cow<'_, str>, Vec<ExtractedLatex>) {
    let mut extracted = vec![];

    let new_text = LATEX.replace_all(text, |caps: &Captures| {
        let latex = match (
            caps.get(1),
            caps.get(2),
            caps.get(3),
            caps.get(4),
            caps.get(5),
        ) {
            (Some(m), _, _, _, _) => m.as_str().into(),
            (_, Some(m), _, _, _) => format!("${}$", m.as_str()),
            (_, _, Some(m), _, _) => {
                format!(r"\begin{{displaymath}}{}\end{{displaymath}}", m.as_str())
            }
            (_, _, _, Some(attrs), Some(body)) => {
                let body = anki_latex_body(attrs.as_str(), body.as_str());
                if anki_latex_is_display(attrs.as_str()) {
                    format!(r"\begin{{displaymath}}{body}\end{{displaymath}}")
                } else {
                    format!("${body}$")
                }
            }
            _ => unreachable!(),
        };
        let latex_text = strip_html_for_latex(&latex);
        let fname = fname_for_latex(&latex_text, svg);
        let img_link = image_link_for_fname(&latex_text, &fname);
        extracted.push(ExtractedLatex {
            fname,
            latex: latex_text.into(),
        });

        img_link
    });

    (new_text, extracted)
}

fn anki_latex_is_display(attributes: &str) -> bool {
    ANKI_LATEX_DISPLAY_KIND.is_match(attributes)
}

fn anki_latex_body(attributes: &str, fallback_body: &str) -> String {
    let Some(caps) = ANKI_LATEX_DATA_ATTR.captures(attributes) else {
        return fallback_body.into();
    };

    for index in 1..=3 {
        if let Some(value) = caps.get(index) {
            return decode_entities(value.as_str()).into_owned();
        }
    }

    fallback_body.into()
}

fn strip_html_for_latex(html: &str) -> Cow<'_, str> {
    let mut out: Cow<str> = html.into();
    if let Cow::Owned(o) = LATEX_NEWLINES.replace_all(html, "\n") {
        out = o.into();
    }
    if let Cow::Owned(o) = strip_html(out.as_ref()) {
        out = o.into();
    }

    out
}

fn fname_for_latex(latex: &str, svg: bool) -> String {
    let ext = if svg { "svg" } else { "png" };
    let csum = hex::encode(sha1_of_data(latex.as_bytes()));

    format!("latex-{csum}.{ext}")
}

fn image_link_for_fname(src: &str, fname: &str) -> String {
    format!(
        "<img class=latex alt=\"{}\" src=\"{}\">",
        htmlescape::encode_attribute(src),
        fname
    )
}

#[cfg(test)]
mod test {
    use crate::latex::extract_latex;
    use crate::latex::ExtractedLatex;

    #[test]
    fn latex() {
        let fname = "latex-ef30b3f4141c33a5bf7044b0d1961d3399c05d50.png";
        assert_eq!(
            extract_latex("a[latex]one<br>and<div>two[/latex]b", false),
            (
                format!("a<img class=latex alt=\"one&#x0A;and&#x0A;two\" src=\"{fname}\">b").into(),
                vec![ExtractedLatex {
                    fname: fname.into(),
                    latex: "one\nand\ntwo".into()
                }]
            )
        );

        assert_eq!(
            extract_latex("[$]<b>hello</b>&nbsp; world[/$]", true).1,
            vec![ExtractedLatex {
                fname: "latex-060219fbf3ddb74306abddaf4504276ad793b029.svg".to_string(),
                latex: "$hello  world$".to_string()
            }]
        );

        assert_eq!(
            extract_latex("[$$]math &amp; stuff[/$$]", false).1,
            vec![ExtractedLatex {
                fname: "latex-8899f3f849ffdef6e4e9f2f34a923a1f608ebc07.png".to_string(),
                latex: r"\begin{displaymath}math & stuff\end{displaymath}".to_string()
            }]
        );

        assert_eq!(
            extract_latex(
                r#"<anki-latex data-latex-kind="inline"><b>hello</b>&nbsp; world</anki-latex>"#,
                true
            )
            .1,
            extract_latex("[$]<b>hello</b>&nbsp; world[/$]", true).1
        );

        assert_eq!(
            extract_latex(
                r#"<anki-latex data-latex-kind="display" data-latex="math &amp; stuff"><span>preview</span></anki-latex>"#,
                false
            )
            .1,
            extract_latex("[$$]math &amp; stuff[/$$]", false).1
        );
    }
}
