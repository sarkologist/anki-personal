// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::BTreeSet;
use std::collections::HashMap;
use std::collections::HashSet;
use std::fmt::Display;
use std::path::Path;
use std::process::Stdio;
use std::sync::LazyLock;
use std::sync::Mutex;

use camino::Utf8PathBuf;
use globset::GlobMatcher;
use regex::Regex;

#[derive(Debug, Clone, Hash, Default)]
pub enum BuildInput {
    Single(String),
    Multiple(Vec<String>),
    Glob(Glob),
    Inputs(Vec<BuildInput>),
    #[default]
    Empty,
}

impl AsRef<BuildInput> for BuildInput {
    fn as_ref(&self) -> &BuildInput {
        self
    }
}

impl From<String> for BuildInput {
    fn from(v: String) -> Self {
        BuildInput::Single(v)
    }
}

impl From<&str> for BuildInput {
    fn from(v: &str) -> Self {
        BuildInput::Single(v.to_owned())
    }
}

impl From<Vec<String>> for BuildInput {
    fn from(v: Vec<String>) -> Self {
        BuildInput::Multiple(v)
    }
}

impl From<Glob> for BuildInput {
    fn from(v: Glob) -> Self {
        BuildInput::Glob(v)
    }
}

impl From<&BuildInput> for BuildInput {
    fn from(v: &BuildInput) -> Self {
        BuildInput::Inputs(vec![v.clone()])
    }
}

impl From<&[BuildInput]> for BuildInput {
    fn from(v: &[BuildInput]) -> Self {
        BuildInput::Inputs(v.to_vec())
    }
}

impl From<Vec<BuildInput>> for BuildInput {
    fn from(v: Vec<BuildInput>) -> Self {
        BuildInput::Inputs(v)
    }
}

impl From<Utf8PathBuf> for BuildInput {
    fn from(v: Utf8PathBuf) -> Self {
        BuildInput::Single(v.into_string())
    }
}

impl BuildInput {
    pub fn add_to_vec(
        &self,
        vec: &mut Vec<String>,
        exisiting_outputs: &HashMap<String, Vec<String>>,
    ) {
        let mut resolve_and_add = |value: &str| {
            if let Some(stripped) = value.strip_prefix(':') {
                let files = exisiting_outputs.get(stripped).unwrap_or_else(|| {
                    println!("{:?}", &exisiting_outputs);
                    panic!("input referenced {value}, but rule missing/not processed");
                });
                for file in files {
                    vec.push(file.into())
                }
            } else {
                vec.push(value.into());
            }
        };

        match self {
            BuildInput::Single(s) => resolve_and_add(s),
            BuildInput::Multiple(v) => {
                for item in v {
                    resolve_and_add(item);
                }
            }
            BuildInput::Glob(glob) => {
                for path in glob.resolve() {
                    vec.push(path.into_string());
                }
            }
            BuildInput::Inputs(inputs) => {
                for input in inputs {
                    input.add_to_vec(vec, exisiting_outputs)
                }
            }
            BuildInput::Empty => {}
        }
    }
}

#[derive(Debug, Clone, Hash)]
pub struct Glob {
    pub include: String,
    pub exclude: Option<String>,
}

/// The source tree. Walking it once instead of for each glob yields ~4x speed
/// improvements.
struct SourceTree {
    files: Vec<Utf8PathBuf>,
    /// Every walked directory, including the root as "".
    dirs: Vec<Utf8PathBuf>,
}

static SOURCE_TREE: LazyLock<SourceTree> = LazyLock::new(walk_source_tree);

/// Directories whose listing some resolved glob depends on. Configure lists
/// them as inputs of build.ninja, so adding, removing or renaming a file in
/// one (which bumps the directory's mtime) regenerates build.ninja.
static WATCHED_DIRS: Mutex<BTreeSet<Utf8PathBuf>> = Mutex::new(BTreeSet::new());

fn walk_source_tree() -> SourceTree {
    let mut tree = SourceTree {
        files: vec![],
        dirs: vec![],
    };
    let walker = walkdir::WalkDir::new(".")
        // ensure the output order is predictable
        .sort_by_file_name()
        .into_iter()
        .filter_entry(|e| {
            // don't walk into symlinks, the top-level out/ or .git, or other
            // git worktrees nested in this one (eg .claude/worktrees/*)
            !(e.path_is_symlink()
                || (e.depth() == 1 && (e.file_name() == "out" || e.file_name() == ".git"))
                || (e.depth() > 0 && e.file_type().is_dir() && is_linked_worktree(e.path())))
        });
    for entry in walker {
        let entry = entry.unwrap();
        let path = entry.path().strip_prefix("./").unwrap();
        let path = Utf8PathBuf::from_path_buf(path.to_owned()).unwrap();
        if entry.file_type().is_dir() {
            tree.dirs.push(path);
        } else {
            tree.files.push(path);
        }
    }
    tree
}

/// True if `dir` is the root of a linked worktree (its `.git` file points into
/// some repo's `.git/worktrees/`). Submodules point into `.git/modules/`.
fn is_linked_worktree(dir: &Path) -> bool {
    let Ok(contents) = std::fs::read_to_string(dir.join(".git")) else {
        return false;
    };
    contents
        .strip_prefix("gitdir:")
        .map(|gitdir| Path::new(gitdir.trim()))
        .and_then(Path::parent)
        .is_some_and(|parent| parent.file_name() == Some("worktrees".as_ref()))
}

/// Watched directories, for use as build inputs. Each is written with a
/// trailing slash (the root as "./"), so that eg the `qt/` folder is not
/// confused with the `qt` phony target; n2 keeps that slash, but Ninja strips
/// it, so this is only for use with n2. Gitignored folders are left out, as
/// builds write to some of them (eg ts/.svelte-kit), and those writes should
/// not trigger a reconfigure.
pub fn watched_dirs() -> Vec<String> {
    let ignored = gitignored_dirs();
    WATCHED_DIRS
        .lock()
        .unwrap()
        .iter()
        .filter(|dir| !dir.ancestors().any(|dir| ignored.contains(dir)))
        .map(|dir| match dir.as_str() {
            "" => "./".to_string(),
            dir => format!("{dir}/"),
        })
        .collect()
}

/// Untracked folders of the source tree that match a gitignore pattern (not
/// their subfolders, which git does not descend into). Empty if git is
/// unavailable.
fn gitignored_dirs() -> HashSet<Utf8PathBuf> {
    let output = std::process::Command::new("git")
        .args(["status", "--porcelain=v1", "-z", "--ignored=matching"])
        .args(["--untracked-files=normal", "--ignore-submodules=all"])
        .stderr(Stdio::null())
        .output();
    let Some(output) = output.ok().filter(|output| output.status.success()) else {
        return Default::default();
    };
    String::from_utf8_lossy(&output.stdout)
        .split('\0')
        .filter_map(|entry| entry.strip_prefix("!! ")?.strip_suffix('/'))
        .map(Utf8PathBuf::from)
        .collect()
}

impl Glob {
    pub fn resolve(&self) -> impl Iterator<Item = Utf8PathBuf> {
        let include = compile_glob(&self.include);
        let exclude = self.exclude.as_deref().map(compile_glob);
        WATCHED_DIRS.lock().unwrap().extend(
            self.dirs_to_watch(&SOURCE_TREE.dirs, exclude.as_ref())
                .into_iter()
                .cloned(),
        );
        SOURCE_TREE.files.iter().filter_map(move |path| {
            if include.is_match(path) {
                let excluded = exclude
                    .as_ref()
                    .map(|exclude| exclude.is_match(path))
                    .unwrap_or_default();
                if !excluded {
                    return Some(path.to_owned());
                }
            }
            None
        })
    }

    /// The directories in `dirs` that a file matching this glob could be added
    /// to: those under the literal prefix of some brace alternative of
    /// `include`, unless `exclude` covers their whole contents. If a prefix
    /// does not exist yet, its nearest existing ancestor is watched instead, so
    /// that creating it is noticed.
    fn dirs_to_watch<'a>(
        &self,
        dirs: &'a [Utf8PathBuf],
        exclude: Option<&GlobMatcher>,
    ) -> Vec<&'a Utf8PathBuf> {
        let prefixes: Vec<Utf8PathBuf> = expand_braces(&self.include)
            .iter()
            .map(|alternative| literal_prefix(alternative))
            .map(|prefix| nearest_existing(prefix, dirs))
            .collect();
        dirs.iter()
            .filter(|dir| {
                prefixes.iter().any(|prefix| dir.starts_with(prefix))
                    && !exclude.is_some_and(|exclude| exclude.is_match(dir.join(EXCLUDE_PROBE)))
            })
            .collect()
    }
}

/// A file name that excludes such as `node_modules/**` match, but that no
/// real exclude would single out.
const EXCLUDE_PROBE: &str = "__glob_exclude_probe__";

fn compile_glob(glob: &str) -> GlobMatcher {
    globset::GlobBuilder::new(glob)
        .literal_separator(true)
        .build()
        .unwrap()
        .compile_matcher()
}

/// Expands the first `{a,b}` group of `glob` (recursively, so all groups),
/// eg `{x,y/z}/**/*.{py,rs}` gives four alternatives.
fn expand_braces(glob: &str) -> Vec<String> {
    let Some(open) = glob.find('{') else {
        return vec![glob.to_string()];
    };
    let mut depth = 0;
    let mut parts = vec![];
    let mut part_start = open + 1;
    for (i, c) in glob.char_indices().skip_while(|(i, _)| *i <= open) {
        match c {
            '{' => depth += 1,
            '}' if depth == 0 => {
                parts.push(&glob[part_start..i]);
                let (head, tail) = (&glob[..open], &glob[i + 1..]);
                return parts
                    .into_iter()
                    .flat_map(|part| expand_braces(&format!("{head}{part}{tail}")))
                    .collect();
            }
            '}' => depth -= 1,
            ',' if depth == 0 => {
                parts.push(&glob[part_start..i]);
                part_start = i + 1;
            }
            _ => {}
        }
    }
    // unbalanced; let globset report it
    vec![glob.to_string()]
}

/// The leading path components of `glob` that contain no glob syntax. For a
/// glob without any, the parent directory of the literal path.
fn literal_prefix(glob: &str) -> Utf8PathBuf {
    let components: Vec<&str> = glob.split('/').collect();
    let literal = components
        .iter()
        .take_while(|c| !c.contains(['*', '?', '[', '{', '\\']))
        .count()
        .min(components.len() - 1);
    components[..literal].iter().collect()
}

fn nearest_existing(mut prefix: Utf8PathBuf, dirs: &[Utf8PathBuf]) -> Utf8PathBuf {
    while !prefix.as_str().is_empty() && !dirs.contains(&prefix) {
        prefix.pop();
    }
    prefix
}

pub fn space_separated<I>(iter: I) -> String
where
    I: IntoIterator,
    I::Item: Display,
{
    itertools::join(iter, " ")
}

/// Join target inputs with a space. Any whitespace characters in the inputs are
/// escaped as `$ `
pub fn join_inputs<I>(iter: I) -> String
where
    I: IntoIterator,
    I::Item: Display,
{
    static WHITESPACE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s").unwrap());
    let iter = iter.into_iter().map(|input| {
        let input = input.to_string();
        WHITESPACE_RE.replace_all(input.trim(), "$$$0").to_string()
    });
    itertools::join(iter, " ")
}

#[cfg(test)]
mod test {
    use super::*;

    fn paths(paths: &[&str]) -> Vec<Utf8PathBuf> {
        paths.iter().map(Utf8PathBuf::from).collect()
    }

    fn watched(glob: Glob, dirs: &[Utf8PathBuf]) -> Vec<&str> {
        let exclude = glob.exclude.as_deref().map(compile_glob);
        glob.dirs_to_watch(dirs, exclude.as_ref())
            .into_iter()
            .map(|dir| dir.as_str())
            .collect()
    }

    #[test]
    fn expands_braces() {
        assert_eq!(expand_braces("a/**/*.py"), ["a/**/*.py"]);
        assert_eq!(
            expand_braces("{x,y/z}/**/*.{py,rs}"),
            ["x/**/*.py", "x/**/*.rs", "y/z/**/*.py", "y/z/**/*.rs"]
        );
        assert_eq!(expand_braces("{a,b{c,d}}/*"), ["a/*", "bc/*", "bd/*"]);
        assert_eq!(expand_braces("a{b"), ["a{b"]);
    }

    #[test]
    fn literal_prefixes() {
        assert_eq!(
            literal_prefix("shell/plugin_host/**/*.py"),
            "shell/plugin_host"
        );
        assert_eq!(literal_prefix("docs-site/*.mdx"), "docs-site");
        assert_eq!(literal_prefix("**/*.svelte"), "");
        assert_eq!(literal_prefix("ts/x?/y"), "ts");
        assert_eq!(literal_prefix("Cargo.lock"), "");
        assert_eq!(literal_prefix("a/b/c.txt"), "a/b");
    }

    #[test]
    fn watches_dirs_a_matching_file_could_be_added_to() {
        let dirs = paths(&[
            "",
            "node_modules",
            "node_modules/x",
            "qt",
            "shell",
            "shell/plugin_host",
            "shell/plugin_host/tests",
            "shell/src",
            "ts",
        ]);
        assert_eq!(
            watched(crate::glob!["shell/plugin_host/**/*.py"], &dirs),
            ["shell/plugin_host", "shell/plugin_host/tests"]
        );
        assert_eq!(
            watched(crate::glob!["{shell/plugin_host,qt}/**"], &dirs),
            ["qt", "shell/plugin_host", "shell/plugin_host/tests"]
        );
        assert_eq!(
            watched(crate::glob!["**/*.ts", "{node_modules,shell}/**"], &dirs),
            ["", "qt", "ts"]
        );
        // a folder that doesn't exist yet is noticed when created
        assert_eq!(
            watched(crate::glob!["shell/new/sub/*.py"], &dirs),
            [
                "shell",
                "shell/plugin_host",
                "shell/plugin_host/tests",
                "shell/src"
            ]
        );
    }

    #[test]
    fn recognises_linked_worktrees() {
        let root = std::env::temp_dir().join(format!("ninja_gen_worktree_{}", std::process::id()));
        let write = |dir: &str, contents: &str| {
            std::fs::create_dir_all(root.join(dir)).unwrap();
            std::fs::write(root.join(dir).join(".git"), contents).unwrap();
        };
        write("worktree", "gitdir: /repo/.git/worktrees/name\n");
        write("relative", "gitdir: ../../.git/worktrees/name\n");
        write("submodule", "gitdir: ../.git/modules/submodule\n");
        write(
            "submodule_in_worktree",
            "gitdir: /repo/.git/worktrees/name/modules/sub\n",
        );
        std::fs::create_dir_all(root.join("plain")).unwrap();

        assert!(is_linked_worktree(&root.join("worktree")));
        assert!(is_linked_worktree(&root.join("relative")));
        assert!(!is_linked_worktree(&root.join("submodule")));
        assert!(!is_linked_worktree(&root.join("submodule_in_worktree")));
        assert!(!is_linked_worktree(&root.join("plain")));
        std::fs::remove_dir_all(root).unwrap();
    }
}
