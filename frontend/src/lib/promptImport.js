export function appendImportedBlocks(existing, imported) {
  const usedIds = new Set(existing.map((block) => block.id));
  // Leave IDs belonging to other incoming blocks available to those blocks.
  const reservedIds = new Set([...usedIds, ...imported.map((block) => block.id)]);
  const additions = imported.map((block) => {
    const baseId = block.id;
    let id = baseId;
    let suffix = 2;
    if (usedIds.has(id)) {
      do {
        id = `${baseId}_${suffix++}`;
      } while (reservedIds.has(id));
    }
    usedIds.add(id);
    reservedIds.add(id);
    return { ...block, id };
  });
  return [...existing, ...additions];
}
