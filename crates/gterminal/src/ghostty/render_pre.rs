impl Drop for KittyPlacementIteratorGuard {
    fn drop(&mut self) {
        unsafe { ffi::ghostty_kitty_graphics_placement_iterator_free(self.raw) }
    }
}

// SAFETY: these opaque handles are only used behind external synchronization in pane runtime.
unsafe impl Send for Terminal {}

impl Drop for Terminal {
    fn drop(&mut self) {
        // SAFETY: freeing a null or live handle is allowed by the C API.
        unsafe {
            ffi::ghostty_terminal_free(self.raw);
        }
    }
}

fn ghostty_viewport_point(x: u16, y: u32) -> ffi::GhosttyPoint {
    ffi::GhosttyPoint {
        tag: ffi::GhosttyPointTag_GHOSTTY_POINT_TAG_VIEWPORT,
        value: ffi::GhosttyPointValue {
            coordinate: ffi::GhosttyPointCoordinate { x, y },
        },
    }
}

fn ghostty_screen_point(x: u16, y: u32) -> ffi::GhosttyPoint {
    ffi::GhosttyPoint {
        tag: ffi::GhosttyPointTag_GHOSTTY_POINT_TAG_SCREEN,
        value: ffi::GhosttyPointValue {
            coordinate: ffi::GhosttyPointCoordinate { x, y },
        },
    }
}

fn grid_ref_graphemes(grid_ref: &ffi::GhosttyGridRef) -> Result<Vec<u32>, Error> {
    let mut required = 0usize;
    let result =
        unsafe { ffi::ghostty_grid_ref_graphemes(grid_ref, ptr::null_mut(), 0, &mut required) };
    if result != ffi::GhosttyResult_GHOSTTY_OUT_OF_SPACE {
        result.into_result()?;
    }
    let mut buffer = vec![0u32; required];
    if required == 0 {
        return Ok(buffer);
    }
    unsafe {
        ffi::ghostty_grid_ref_graphemes(grid_ref, buffer.as_mut_ptr(), buffer.len(), &mut required)
            .into_result()?;
    }
    buffer.truncate(required);
    Ok(buffer)
}

fn grid_ref_wide(grid_ref: &ffi::GhosttyGridRef) -> Result<CellWide, Error> {
    let mut raw = ffi::GhosttyCell::default();
    unsafe {
        ffi::ghostty_grid_ref_cell(grid_ref, &mut raw).into_result()?;
    }

    let mut wide = ffi::GhosttyCellWide_GHOSTTY_CELL_WIDE_NARROW;
    unsafe {
        ffi::ghostty_cell_get(
            raw,
            ffi::GhosttyCellData_GHOSTTY_CELL_DATA_WIDE,
            (&mut wide as *mut ffi::GhosttyCellWide).cast(),
        )
        .into_result()?;
    }
    Ok(CellWide::from_raw(wide))
}

fn grid_ref_wrap_state(grid_ref: &ffi::GhosttyGridRef) -> Result<(bool, bool), Error> {
    let mut row = 0;
    unsafe {
        ffi::ghostty_grid_ref_row(grid_ref, &mut row).into_result()?;
    }
    let mut soft_wrapped = false;
    let mut wrap_continuation = false;
    unsafe {
        ffi::ghostty_row_get(
            row,
            ffi::GhosttyRowData_GHOSTTY_ROW_DATA_WRAP,
            (&mut soft_wrapped as *mut bool).cast(),
        )
        .into_result()?;
        ffi::ghostty_row_get(
            row,
            ffi::GhosttyRowData_GHOSTTY_ROW_DATA_WRAP_CONTINUATION,
            (&mut wrap_continuation as *mut bool).cast(),
        )
        .into_result()?;
    }
    Ok((soft_wrapped, wrap_continuation))
}

fn kitty_placement_u32(
    iterator: ffi::GhosttyKittyGraphicsPlacementIterator,
    data: ffi::GhosttyKittyGraphicsPlacementData,
) -> Result<u32, Error> {
    let mut out = 0u32;
    unsafe {
        ffi::ghostty_kitty_graphics_placement_get(iterator, data, (&mut out as *mut u32).cast())
            .into_result()?;
    }
    Ok(out)
}

fn kitty_placement_i32(
    iterator: ffi::GhosttyKittyGraphicsPlacementIterator,
    data: ffi::GhosttyKittyGraphicsPlacementData,
) -> Result<i32, Error> {
    let mut out = 0i32;
    unsafe {
        ffi::ghostty_kitty_graphics_placement_get(iterator, data, (&mut out as *mut i32).cast())
            .into_result()?;
    }
    Ok(out)
}

fn kitty_placement_bool(
    iterator: ffi::GhosttyKittyGraphicsPlacementIterator,
    data: ffi::GhosttyKittyGraphicsPlacementData,
) -> Result<bool, Error> {
    let mut out = false;
    unsafe {
        ffi::ghostty_kitty_graphics_placement_get(iterator, data, (&mut out as *mut bool).cast())
            .into_result()?;
    }
    Ok(out)
}

fn kitty_virtual_placement_specs(
    graphics: ffi::GhosttyKittyGraphics,
) -> Result<Vec<KittyVirtualPlacementSpec>, Error> {
    let mut iterator: ffi::GhosttyKittyGraphicsPlacementIterator = ptr::null_mut();
    unsafe {
        ffi::ghostty_kitty_graphics_placement_iterator_new(ptr::null(), &mut iterator)
            .into_result()?;
        ffi::ghostty_kitty_graphics_get(
            graphics,
            ffi::GhosttyKittyGraphicsData_GHOSTTY_KITTY_GRAPHICS_DATA_PLACEMENT_ITERATOR,
            (&mut iterator as *mut ffi::GhosttyKittyGraphicsPlacementIterator).cast(),
        )
        .into_result()?;
    }
    let _guard = KittyPlacementIteratorGuard { raw: iterator };

    let mut specs = Vec::new();
    while unsafe { ffi::ghostty_kitty_graphics_placement_next(iterator) } {
        if !kitty_placement_bool(iterator, KITTY_PLACEMENT_DATA_IS_VIRTUAL)? {
            continue;
        }
        specs.push(KittyVirtualPlacementSpec {
            image_id: kitty_placement_u32(
                iterator,
                ffi::GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IMAGE_ID,
            )?,
            placement_id: kitty_placement_u32(
                iterator,
                ffi::GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_PLACEMENT_ID,
            )?,
            columns: kitty_placement_u32(iterator, KITTY_PLACEMENT_DATA_COLUMNS)?,
            rows: kitty_placement_u32(iterator, KITTY_PLACEMENT_DATA_ROWS)?,
            z: kitty_placement_i32(
                iterator,
                ffi::GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Z,
            )?,
        });
    }
    Ok(specs)
}

fn find_virtual_placement_spec(
    specs: &[KittyVirtualPlacementSpec],
    image_id: u32,
    placement_id: u32,
) -> Option<&KittyVirtualPlacementSpec> {
    if placement_id > 0 {
        specs
            .iter()
            .find(|spec| spec.image_id == image_id && spec.placement_id == placement_id)
    } else {
        specs.iter().find(|spec| spec.image_id == image_id)
    }
}

fn kitty_virtual_cell(
    x: u16,
    y: u16,
    graphemes: &[u32],
    style: CellStyle,
) -> Option<KittyVirtualCell> {
    if graphemes.first().copied() != Some(KITTY_UNICODE_PLACEHOLDER) {
        return None;
    }
    let image_id_low = style
        .fg_color
        .map(kitty_placeholder_color_to_id)
        .unwrap_or(0);
    let placement_id = style
        .underline_color
        .map(kitty_placeholder_color_to_id)
        .filter(|id| *id != 0);
    let row = graphemes
        .get(1)
        .and_then(|codepoint| kitty_placeholder_diacritic_index(*codepoint));
    let col = graphemes
        .get(2)
        .and_then(|codepoint| kitty_placeholder_diacritic_index(*codepoint));
    let image_id_high = graphemes
        .get(3)
        .and_then(|codepoint| kitty_placeholder_diacritic_index(*codepoint))
        .filter(|high| *high <= u32::from(u8::MAX));

    Some(KittyVirtualCell {
        x,
        y,
        image_id_low,
        image_id_high,
        placement_id,
        row,
        col,
    })
}

fn kitty_placeholder_color_to_id(color: CellColor) -> u32 {
    match color {
        CellColor::Palette(value) => value.into(),
        CellColor::Rgb(color) => {
            (u32::from(color.r) << 16) | (u32::from(color.g) << 8) | u32::from(color.b)
        }
    }
}

fn kitty_placeholder_diacritic_index(codepoint: u32) -> Option<u32> {
    let map = KITTY_PLACEHOLDER_DIACRITICS.get_or_init(|| {
        // Reuse Ghostty's vendored table so Gterm decodes the same placeholder
        // row/column diacritics that libghostty accepts.
        let source =
            include_str!("../../vendor/libghostty-vt/src/terminal/kitty/graphics_unicode.zig");
        let mut map = HashMap::new();
        let mut in_table = false;
        for line in source.lines() {
            let line = line.trim();
            if line.starts_with("const diacritics:") {
                in_table = true;
                continue;
            }
            if !in_table {
                continue;
            }
            if line == "};" {
                break;
            }
            let Some(hex) = line
                .strip_prefix("0x")
                .and_then(|value| value.strip_suffix(','))
            else {
                continue;
            };
            if let Ok(value) = u32::from_str_radix(hex, 16) {
                map.insert(value, map.len() as u32);
            }
        }
        map
    });
    map.get(&codepoint).copied()
}

fn kitty_virtual_placement_geometry(
    run: KittyVirtualRun,
    spec: KittyVirtualPlacementSpec,
    image_width: u32,
    image_height: u32,
    cell_width: u32,
    cell_height: u32,
) -> Option<KittyVirtualPlacementGeometry> {
    let grid_cols = if spec.columns == 0 {
        image_width.saturating_add(cell_width - 1) / cell_width
    } else {
        spec.columns
    }
    .max(1);
    let grid_rows = if spec.rows == 0 {
        image_height.saturating_add(cell_height - 1) / cell_height
    } else {
        spec.rows
    }
    .max(1);

    if run.col >= grid_cols || run.row >= grid_rows {
        return None;
    }
    let visible_cols = run.width.min(grid_cols.saturating_sub(run.col)).max(1);
    let visible_rows = 1;
    let source_x = scale_u32(run.col, image_width, grid_cols);
    let source_y = scale_u32(run.row, image_height, grid_rows);
    let source_width = scale_u32(visible_cols, image_width, grid_cols)
        .max(1)
        .min(image_width.saturating_sub(source_x));
    let source_height = scale_u32(visible_rows, image_height, grid_rows)
        .max(1)
        .min(image_height.saturating_sub(source_y));
    if source_width == 0 || source_height == 0 {
        return None;
    }

    Some(KittyVirtualPlacementGeometry {
        x_offset: 0,
        y_offset: 0,
        render: KittyPlacementRenderInfo {
            pixel_width: visible_cols.saturating_mul(cell_width).max(1),
            pixel_height: visible_rows.saturating_mul(cell_height).max(1),
            grid_cols: visible_cols,
            grid_rows: visible_rows,
            viewport_col: i32::from(run.x),
            viewport_row: i32::from(run.y),
            source_x,
            source_y,
            source_width,
            source_height,
        },
    })
}

fn scale_u32(value: u32, source: u32, dest: u32) -> u32 {
    ((u64::from(value)).saturating_mul(u64::from(source)) / u64::from(dest.max(1)))
        .min(u64::from(u32::MAX)) as u32
}

impl KittyVirtualRun {
    fn from_cell(cell: KittyVirtualCell) -> Self {
        Self {
            x: cell.x,
            y: cell.y,
            image_id_low: cell.image_id_low,
            image_id_high: cell.image_id_high,
            placement_id: cell.placement_id,
            row: cell.row.unwrap_or(0),
            col: cell.col.unwrap_or(0),
            width: 1,
        }
    }

    fn append(&mut self, cell: KittyVirtualCell) -> bool {
        if self.image_id_low != cell.image_id_low
            || self.placement_id != cell.placement_id
            || cell.row.is_some_and(|row| row != self.row)
            || cell.col.is_some_and(|col| col != self.col + self.width)
            || cell
                .image_id_high
                .is_some_and(|high| Some(high) != self.image_id_high)
        {
            return false;
        }
        self.width += 1;
        true
    }

    fn image_id(self) -> u32 {
        self.image_id_low | (self.image_id_high.unwrap_or(0) << 24)
    }

    fn placement_id(self) -> u32 {
        self.placement_id.unwrap_or(0)
    }

    fn synthetic_placement_id(self) -> u32 {
        let mut hasher = DefaultHasher::new();
        self.image_id().hash(&mut hasher);
        self.placement_id().hash(&mut hasher);
        self.row.hash(&mut hasher);
        self.col.hash(&mut hasher);
        self.width.hash(&mut hasher);
        self.x.hash(&mut hasher);
        self.y.hash(&mut hasher);
        1 + ((hasher.finish() as u32) % 900_000)
    }
}

fn kitty_graphics_u64(
    graphics: ffi::GhosttyKittyGraphics,
    data: ffi::GhosttyKittyGraphicsData,
) -> Result<u64, Error> {
    let mut out = 0u64;
    unsafe {
        ffi::ghostty_kitty_graphics_get(graphics, data, (&mut out as *mut u64).cast())
            .into_result()?;
    }
    Ok(out)
}

fn kitty_image_u32(
    image: ffi::GhosttyKittyGraphicsImage,
    data: ffi::GhosttyKittyGraphicsImageData,
) -> Result<u32, Error> {
    let mut out = 0u32;
    unsafe {
        ffi::ghostty_kitty_graphics_image_get(image, data, (&mut out as *mut u32).cast())
            .into_result()?;
    }
    Ok(out)
}

fn kitty_image_u64(
    image: ffi::GhosttyKittyGraphicsImage,
    data: ffi::GhosttyKittyGraphicsImageData,
) -> Result<u64, Error> {
    let mut out = 0u64;
    unsafe {
        ffi::ghostty_kitty_graphics_image_get(image, data, (&mut out as *mut u64).cast())
            .into_result()?;
    }
    Ok(out)
}

fn kitty_image_format(image: ffi::GhosttyKittyGraphicsImage) -> Result<KittyImageFormat, Error> {
    let mut out = ffi::GhosttyKittyImageFormat_GHOSTTY_KITTY_IMAGE_FORMAT_RGBA;
    unsafe {
        ffi::ghostty_kitty_graphics_image_get(
            image,
            ffi::GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_FORMAT,
            (&mut out as *mut ffi::GhosttyKittyImageFormat).cast(),
        )
        .into_result()?;
    }
    match out {
        ffi::GhosttyKittyImageFormat_GHOSTTY_KITTY_IMAGE_FORMAT_RGB => Ok(KittyImageFormat::Rgb),
        ffi::GhosttyKittyImageFormat_GHOSTTY_KITTY_IMAGE_FORMAT_RGBA => Ok(KittyImageFormat::Rgba),
        ffi::GhosttyKittyImageFormat_GHOSTTY_KITTY_IMAGE_FORMAT_PNG => Ok(KittyImageFormat::Png),
        _ => Err(Error(ffi::GhosttyResult_GHOSTTY_INVALID_VALUE)),
    }
}

fn kitty_image_compression(
    image: ffi::GhosttyKittyGraphicsImage,
) -> Result<ffi::GhosttyKittyImageCompression, Error> {
    let mut out = ffi::GhosttyKittyImageCompression_GHOSTTY_KITTY_IMAGE_COMPRESSION_NONE;
    unsafe {
        ffi::ghostty_kitty_graphics_image_get(
            image,
            ffi::GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_COMPRESSION,
            (&mut out as *mut ffi::GhosttyKittyImageCompression).cast(),
        )
        .into_result()?;
    }
    Ok(out)
}

fn kitty_image_data_ptr_len(
    image: ffi::GhosttyKittyGraphicsImage,
) -> Result<(*const u8, usize), Error> {
    let mut ptr_out: *const u8 = ptr::null();
    let mut len = 0usize;
    unsafe {
        ffi::ghostty_kitty_graphics_image_get(
            image,
            ffi::GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_DATA_PTR,
            (&mut ptr_out as *mut *const u8).cast(),
        )
        .into_result()?;
        ffi::ghostty_kitty_graphics_image_get(
            image,
            ffi::GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_DATA_LEN,
            (&mut len as *mut usize).cast(),
        )
        .into_result()?;
    }
    Ok((ptr_out, len))
}

fn kitty_image_data_from_ptr(ptr_out: *const u8, len: usize) -> Vec<u8> {
    if ptr_out.is_null() || len == 0 {
        return Vec::new();
    }
    unsafe { slice::from_raw_parts(ptr_out, len) }.to_vec()
}

// Hashes the full payload. Callers cache the result per image id and only
// recompute it when the image's transmit time changes.
fn kitty_image_fingerprint(
    ptr_out: *const u8,
    len: usize,
    image_width: u32,
    image_height: u32,
    format: KittyImageFormat,
) -> u64 {
    let mut hasher = DefaultHasher::new();
    len.hash(&mut hasher);
    image_width.hash(&mut hasher);
    image_height.hash(&mut hasher);
    format.hash(&mut hasher);
    if ptr_out.is_null() || len == 0 {
        return hasher.finish();
    }

    let data = unsafe { slice::from_raw_parts(ptr_out, len) };
    data.hash(&mut hasher);
    hasher.finish()
}

fn grid_ref_hyperlink_uri(grid_ref: &ffi::GhosttyGridRef) -> Result<Option<String>, Error> {
    let mut required = 0usize;
    let result =
        unsafe { ffi::ghostty_grid_ref_hyperlink_uri(grid_ref, ptr::null_mut(), 0, &mut required) };
    if result != ffi::GhosttyResult_GHOSTTY_OUT_OF_SPACE {
        result.into_result()?;
    }
    if required == 0 {
        return Ok(None);
    }
    let mut buffer = vec![0u8; required];
    unsafe {
        ffi::ghostty_grid_ref_hyperlink_uri(
            grid_ref,
            buffer.as_mut_ptr(),
            buffer.len(),
            &mut required,
        )
        .into_result()?;
    }
    buffer.truncate(required);
    Ok(Some(String::from_utf8_lossy(&buffer).into_owned()))
}

