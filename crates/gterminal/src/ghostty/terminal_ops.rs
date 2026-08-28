impl Terminal {
    pub fn kitty_image_placements_with_data_filter<F>(
        &self,
        mut needs_data: F,
    ) -> Result<Vec<KittyImagePlacement>, Error>
    where
        F: FnMut(KittyImageDescriptor) -> bool,
    {
        let graphics = self.kitty_graphics()?;
        if graphics.is_null() {
            return Ok(Vec::new());
        }
        let generation = kitty_graphics_u64(
            graphics,
            ffi::GhosttyKittyGraphicsData_GHOSTTY_KITTY_GRAPHICS_DATA_GENERATION,
        )?;
        if generation == 0 || self.kitty_empty_generation.get() == Some(generation) {
            return Ok(Vec::new());
        }

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

        let mut placements = Vec::new();
        let mut storage_has_placements = false;
        while unsafe { ffi::ghostty_kitty_graphics_placement_next(iterator) } {
            storage_has_placements = true;
            if let Some(placement) =
                self.kitty_image_placement(graphics, iterator, &mut needs_data)?
            {
                placements.push(placement);
            }
        }
        if !storage_has_placements {
            self.kitty_empty_generation.set(Some(generation));
            self.prune_kitty_fingerprints(&[]);
            return Ok(Vec::new());
        }

        placements.extend(self.kitty_virtual_image_placements(graphics, &mut needs_data)?);
        placements.sort_by_key(|placement| placement.z);
        self.prune_kitty_fingerprints(&placements);
        Ok(placements)
    }

    /// Fingerprint for `image`, cached per image id and recomputed only when
    /// the image's generation changes.
    fn kitty_image_fingerprint_cached(
        &self,
        image: ffi::GhosttyKittyGraphicsImage,
        image_id: u32,
        data: (*const u8, usize),
        image_width: u32,
        image_height: u32,
        format: KittyImageFormat,
    ) -> u64 {
        let (data_ptr, data_len) = data;
        let Ok(generation) = kitty_image_u64(
            image,
            ffi::GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_GENERATION,
        ) else {
            return kitty_image_fingerprint(data_ptr, data_len, image_width, image_height, format);
        };

        if let Ok(cache) = self.kitty_fingerprints.lock() {
            if let Some(entry) = cache.get(&image_id) {
                if entry.generation == generation {
                    return entry.fingerprint;
                }
            }
        }

        let fingerprint =
            kitty_image_fingerprint(data_ptr, data_len, image_width, image_height, format);
        if let Ok(mut cache) = self.kitty_fingerprints.lock() {
            cache.insert(
                image_id,
                KittyImageFingerprintEntry {
                    generation,
                    fingerprint,
                },
            );
        }
        fingerprint
    }

    fn prune_kitty_fingerprints(&self, placements: &[KittyImagePlacement]) {
        if let Ok(mut cache) = self.kitty_fingerprints.lock() {
            if cache.is_empty() {
                return;
            }
            let live: HashSet<u32> = placements
                .iter()
                .map(|placement| placement.image_id)
                .collect();
            cache.retain(|image_id, _| live.contains(image_id));
        }
    }

    fn kitty_image_placement<F>(
        &self,
        graphics: ffi::GhosttyKittyGraphics,
        iterator: ffi::GhosttyKittyGraphicsPlacementIterator,
        needs_data: &mut F,
    ) -> Result<Option<KittyImagePlacement>, Error>
    where
        F: FnMut(KittyImageDescriptor) -> bool,
    {
        let image_id = kitty_placement_u32(
            iterator,
            ffi::GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IMAGE_ID,
        )?;
        if kitty_placement_bool(iterator, KITTY_PLACEMENT_DATA_IS_VIRTUAL)? {
            return Ok(None);
        }
        let image = unsafe { ffi::ghostty_kitty_graphics_image(graphics, image_id) };
        if image.is_null() {
            return Ok(None);
        }

        let mut raw_info = ffi::GhosttyKittyGraphicsPlacementRenderInfo {
            size: mem::size_of::<ffi::GhosttyKittyGraphicsPlacementRenderInfo>(),
            ..Default::default()
        };
        unsafe {
            ffi::ghostty_kitty_graphics_placement_render_info(
                iterator,
                image,
                self.raw,
                &mut raw_info,
            )
            .into_result()?;
        }
        if !raw_info.viewport_visible {
            return Ok(None);
        }

        let image_width = kitty_image_u32(
            image,
            ffi::GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_WIDTH,
        )?;
        let image_height = kitty_image_u32(
            image,
            ffi::GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_HEIGHT,
        )?;
        let format = kitty_image_format(image)?;
        let compression = kitty_image_compression(image)?;
        if compression != ffi::GhosttyKittyImageCompression_GHOSTTY_KITTY_IMAGE_COMPRESSION_NONE {
            return Ok(None);
        }
        let placement_id = kitty_placement_u32(
            iterator,
            ffi::GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_PLACEMENT_ID,
        )?;
        let (data_ptr, data_len) = kitty_image_data_ptr_len(image)?;
        let data_fingerprint = self.kitty_image_fingerprint_cached(
            image,
            image_id,
            (data_ptr, data_len),
            image_width,
            image_height,
            format,
        );
        let descriptor = KittyImageDescriptor {
            image_id,
            placement_id,
            image_width,
            image_height,
            format,
            data_len,
            data_fingerprint,
        };
        let data = if needs_data(descriptor) {
            kitty_image_data_from_ptr(data_ptr, data_len)
        } else {
            Vec::new()
        };
        let x_offset = kitty_placement_u32(
            iterator,
            ffi::GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_X_OFFSET,
        )?;
        let y_offset = kitty_placement_u32(
            iterator,
            ffi::GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Y_OFFSET,
        )?;
        let z = kitty_placement_i32(
            iterator,
            ffi::GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Z,
        )?;

        Ok(Some(KittyImagePlacement {
            image_id,
            placement_id,
            z,
            x_offset,
            y_offset,
            image_width,
            image_height,
            format,
            data_len,
            data_fingerprint,
            data,
            render: KittyPlacementRenderInfo {
                pixel_width: raw_info.pixel_width,
                pixel_height: raw_info.pixel_height,
                grid_cols: raw_info.grid_cols,
                grid_rows: raw_info.grid_rows,
                viewport_col: raw_info.viewport_col,
                viewport_row: raw_info.viewport_row,
                source_x: raw_info.source_x,
                source_y: raw_info.source_y,
                source_width: raw_info.source_width,
                source_height: raw_info.source_height,
            },
        }))
    }

    fn kitty_virtual_image_placements<F>(
        &self,
        graphics: ffi::GhosttyKittyGraphics,
        needs_data: &mut F,
    ) -> Result<Vec<KittyImagePlacement>, Error>
    where
        F: FnMut(KittyImageDescriptor) -> bool,
    {
        let specs = kitty_virtual_placement_specs(graphics)?;
        if specs.is_empty() {
            return Ok(Vec::new());
        }

        let viewport_cols = self.cols()?.max(1);
        let viewport_rows = self.rows()?.max(1);
        let cell_width = (self.width_px()? / u32::from(viewport_cols)).max(1);
        let cell_height = (self.height_px()? / u32::from(viewport_rows)).max(1);
        let mut runs = Vec::new();
        for y in 0..viewport_rows {
            let mut current: Option<KittyVirtualRun> = None;
            for x in 0..viewport_cols {
                let (graphemes, style) = self.viewport_graphemes_and_style(x, u32::from(y))?;
                let cell = kitty_virtual_cell(x, y, &graphemes, style);
                match cell {
                    Some(cell) => {
                        if let Some(run) = current.as_mut() {
                            if run.append(cell) {
                                continue;
                            }
                            runs.push(*run);
                        }
                        current = Some(KittyVirtualRun::from_cell(cell));
                    }
                    None => {
                        if let Some(run) = current.take() {
                            runs.push(run);
                        }
                    }
                }
            }
            if let Some(run) = current {
                runs.push(run);
            }
        }

        let mut placements = Vec::new();
        for run in runs {
            let image_id = run.image_id();
            let Some(spec) = find_virtual_placement_spec(&specs, image_id, run.placement_id())
            else {
                continue;
            };
            let image = unsafe { ffi::ghostty_kitty_graphics_image(graphics, image_id) };
            if image.is_null() {
                continue;
            }
            let image_width = kitty_image_u32(
                image,
                ffi::GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_WIDTH,
            )?;
            let image_height = kitty_image_u32(
                image,
                ffi::GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_HEIGHT,
            )?;
            let format = kitty_image_format(image)?;
            let compression = kitty_image_compression(image)?;
            if compression != ffi::GhosttyKittyImageCompression_GHOSTTY_KITTY_IMAGE_COMPRESSION_NONE
            {
                continue;
            }
            let Some(geometry) = kitty_virtual_placement_geometry(
                run,
                *spec,
                image_width,
                image_height,
                cell_width,
                cell_height,
            ) else {
                continue;
            };
            let placement_id = run.synthetic_placement_id();
            let (data_ptr, data_len) = kitty_image_data_ptr_len(image)?;
            let data_fingerprint = self.kitty_image_fingerprint_cached(
                image,
                image_id,
                (data_ptr, data_len),
                image_width,
                image_height,
                format,
            );
            let descriptor = KittyImageDescriptor {
                image_id,
                placement_id,
                image_width,
                image_height,
                format,
                data_len,
                data_fingerprint,
            };
            let data = if needs_data(descriptor) {
                kitty_image_data_from_ptr(data_ptr, data_len)
            } else {
                Vec::new()
            };
            placements.push(KittyImagePlacement {
                image_id,
                placement_id,
                z: spec.z,
                x_offset: geometry.x_offset,
                y_offset: geometry.y_offset,
                image_width,
                image_height,
                format,
                data_len,
                data_fingerprint,
                data,
                render: geometry.render,
            });
        }

        Ok(placements)
    }

    fn raw(&self) -> ffi::GhosttyTerminal {
        self.raw
    }
}

struct KittyPlacementIteratorGuard {
    raw: ffi::GhosttyKittyGraphicsPlacementIterator,
}

