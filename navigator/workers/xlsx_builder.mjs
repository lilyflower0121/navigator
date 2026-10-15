// Internal worker. Python validates ZIP/XML and owns all private paths.
import fs from 'node:fs/promises';
import path from 'node:path';
const { FileBlob, SpreadsheetFile, Workbook } = await import(process.env.NAVIGATOR_ARTIFACT_TOOL_MODULE || '@oai/artifact-tool');

const directory = process.argv[2];
if (!directory || process.argv.length !== 3) throw new Error('internal_arguments');
const config = JSON.parse(await fs.readFile(path.join(directory, 'request.json'), 'utf8'));
const workbook = config.mode === 'fixture' ? Workbook.create() : await SpreadsheetFile.importXlsx(await FileBlob.load(path.join(directory, 'input.xlsx')));
if (config.mode === 'fixture') {
  const sheet = workbook.worksheets.add('Fixture');
  sheet.getRange('A1:C3').values = [['Heading', null, null], ['unchanged', 'before', 0], [null, false, null]];
  sheet.mergeCells('A1:C1');
  sheet.getRange('A1:C1').format.fill = '#123456';
  sheet.getRange('A1:C1').format.font = {bold: true, color: '#FFFFFF'};
  sheet.getRange('A1:C3').format.columnWidth = 18;
  sheet.getRange('A1:C3').format.rowHeight = 24;
  await (await SpreadsheetFile.exportXlsx(workbook)).save(path.join(directory, 'output.xlsx'));
} else
if (config.mode === 'render') {
  const preview = await workbook.render({sheetName: config.sheet, range: config.range, scale: 1, format: 'png'});
  await fs.writeFile(path.join(directory, 'preview.png'), new Uint8Array(await preview.arrayBuffer()));
} else if (config.mode === 'patch') {
  for (const {sheet, cell, value} of config.cells) {
    // Documented literal convention prevents text becoming a formula.
    const literal = typeof value === 'string' && (value.startsWith('=') || value.startsWith("'=")) ? `'${value}` : value;
    workbook.worksheets.getItem(sheet).getRange(cell).values = [[literal]];
  }
  for (const adjustment of config.rowHeights ?? []) {
    workbook.worksheets.getItem(adjustment.sheet).getRange(`A${adjustment.row}:H${adjustment.row}`).format.rowHeight = adjustment.height;
  }
  workbook.recalculate();
  await (await SpreadsheetFile.exportXlsx(workbook)).save(path.join(directory, 'output.xlsx'));
} else {
  throw new Error('internal_mode');
}
