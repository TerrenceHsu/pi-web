import type { FileRef } from "../types"

export interface SessionFileTreeNode {
  kind: "directory" | "file"
  name: string
  path: string
  children: SessionFileTreeNode[]
  file?: FileRef
}

export function buildSessionFileTree(files: FileRef[]): SessionFileTreeNode[] {
  const root: SessionFileTreeNode = {
    kind: "directory",
    name: "Session",
    path: "",
    children: [],
  }

  for (const file of files) {
    const logicalPath = (file.logical_path || file.name).replaceAll("\\", "/")
    const parts = logicalPath.split("/").filter(Boolean)
    if (!parts.length) parts.push(file.name)
    let parent = root
    let currentPath = ""

    for (const folder of parts.slice(0, -1)) {
      currentPath = currentPath ? `${currentPath}/${folder}` : folder
      let directory = parent.children.find(
        (child) => child.kind === "directory" && child.name === folder,
      )
      if (!directory) {
        directory = {
          kind: "directory",
          name: folder,
          path: currentPath,
          children: [],
        }
        parent.children.push(directory)
      }
      parent = directory
    }

    parent.children.push({
      kind: "file",
      name: parts.at(-1) || file.name,
      path: logicalPath,
      children: [],
      file,
    })
  }

  function sortChildren(node: SessionFileTreeNode): void {
    node.children.sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === "directory" ? -1 : 1
      const leftAgent = left.file?.purpose === "agent_instructions"
      const rightAgent = right.file?.purpose === "agent_instructions"
      if (leftAgent !== rightAgent) return leftAgent ? -1 : 1
      return left.name.localeCompare(right.name)
    })
    node.children.forEach(sortChildren)
  }
  sortChildren(root)
  return root.children
}
