# $autorun

module PocketThemes
  ROOT = "view_menu.pocket_tools"
  MENU = "#{ROOT}.themes"
  THEMES = {
    "dark" => {
      title: "Dark", window: "#252525", base: "#181818", alternate: "#303030",
      button: "#333333", text: "#eeeeee", muted: "#9a9a9a", accent: "#366ca8",
      link: "#8dbdf5"
    },
    "light" => {
      title: "Light", window: "#f0f0f0", base: "#ffffff", alternate: "#f4f6f8",
      button: "#e6e9ed", text: "#20242a", muted: "#626a73", accent: "#285b91",
      link: "#18568d"
    },
    "graphite" => {
      title: "Graphite", window: "#3a3d41", base: "#2e3033", alternate: "#414448",
      button: "#484c51", text: "#f1f2f3", muted: "#b1b6bc", accent: "#466892",
      link: "#a6c8f2"
    },
    "solarized_dark" => {
      title: "Solarized Dark", window: "#073642", base: "#002b36", alternate: "#0b3c47",
      button: "#114552", text: "#d2dddd", muted: "#93a1a1", accent: "#235f84",
      link: "#85c6ee"
    },
    "solarized_light" => {
      title: "Solarized Light", window: "#eee8d5", base: "#fdf6e3", alternate: "#f3edda",
      button: "#e4ddc8", text: "#344c56", muted: "#64747a", accent: "#245e83",
      link: "#18568d"
    }
  }.freeze

  # KLayout's built-in ColorPalette (layColorPalette.cc). The config entry is
  # empty for this default palette; the toolbox stores it internally in C++.
  DEFAULT_LAYER_COLORS = %w[
    255,157,157 255,128,168 192,128,255 149,128,255 128,134,255 128,168,255
    255,0,0 255,0,128 255,0,255 128,0,255 0,0,255 0,128,255
    128,0,0 128,0,87 128,0,128 80,0,128 0,0,128 0,64,128
    128,255,251 128,255,141 175,255,128 243,255,128 255,194,128 255,160,128
    0,255,255 1,255,107 145,255,0 221,255,0 255,174,0 255,128,0
    0,128,128 0,128,80 0,128,0 80,128,0 128,128,0 128,80,0
    255,255,255 192,192,192 128,128,128 96,96,96 64,64,64 0,0,0
  ].freeze

  def self.layer_colors
    configured = RBA::Application.instance.get_config("color-palette").to_s
    entries = configured.empty? ? DEFAULT_LAYER_COLORS : configured.split
    entries.map do |entry|
      match = /\A(\d+),(\d+),(\d+)(?:\[\d+\])?\z/.match(entry)
      next unless match
      RBA::QColor.new(*match.captures.first(3).map(&:to_i))
    end
  end

  class ToolboxSwatchHover < RBA::QObject
    def eventFilter(widget, event)
      if event.type == RBA::QEvent::Enter
        widget.setProperty("pocket_hovered", true)
        widget.setLineWidth(2)
      elsif event.type == RBA::QEvent::Leave
        widget.setProperty("pocket_hovered", false)
        widget.setLineWidth(widget.property("pocket_selected") ? 2 : 1)
      end
      false
    end
  end

  # KLayout paints color samples with each label's own Window palette role.
  # Restore that role after theme changes; style the remaining chrome roles.
  def self.style_layer_toolbox(dock, name)
    colors = THEMES.fetch(name)
    widgets = [dock]
    headers = []
    walk = lambda do |parent|
      parent.children.each do |child|
        widgets << child if child.respond_to?(:palette) && child.respond_to?(:setPalette)
        headers << child if parent.objectName == "lt" && child.objectName == "panel"
        walk.call(child)
      end
    end
    walk.call(dock)
    sample = widgets.find do |widget|
      widget.objectName == "button" && widget.respond_to?(:text) && widget.text.to_s.empty? &&
        widget.parent && widget.parent.objectName == "color_f" &&
        widget.parent.parent && %w[colors colors_frame].include?(widget.parent.parent.objectName)
    end
    if dock.property("pocket_tools_theme").to_s == name && sample &&
       sample.palette.color(RBA::QPalette::Light).name == colors[:alternate] &&
       sample.palette.color(RBA::QPalette::Window).name == layer_colors.first.name
      return
    end

    roles = {
      WindowText: :text, Text: :text, ButtonText: :text,
      Highlight: :button, HighlightedText: :text,
      Light: :alternate, Midlight: :alternate,
      Mid: :window, Dark: :base, Shadow: :base
    }
    swatches = layer_colors
    swatch_positions = Hash.new(0)
    widgets.each do |widget|
      palette = RBA::QPalette.new(widget.palette)
      roles.each do |role, key|
        palette.setColor(RBA::QPalette.const_get(role), RBA::QColor.new(colors.fetch(key)))
      end
      if widget.objectName == "button" && widget.parent && widget.parent.objectName == "color_f"
        section = widget.parent.parent
        if section && %w[colors colors_frame].include?(section.objectName)
          row = widget.parent.parent.children.select { |child| child.objectName == "color_f" }.index(widget.parent)
          column = swatch_positions[widget.parent.object_id]
          swatch_positions[widget.parent.object_id] += 1
          color = swatches[column * 6 + row] if row
          palette.setColor(RBA::QPalette::Window, color) if color
        end
      end
      widget.setPalette(palette)
    end
    unless dock.property("pocket_hover_ready")
      hover_filter = ToolboxSwatchHover.new(dock)
      @toolbox_hover_filters ||= {}
      key = dock.object_id
      @toolbox_hover_filters[key] = hover_filter
      dock.destroyed { @toolbox_hover_filters.delete(key) }
      widgets.each do |widget|
        next unless widget.objectName == "button" && widget.respond_to?(:text) && widget.text.to_s.empty?
        next unless widget.parent && widget.parent.objectName == "color_f"
        widget.installEventFilter(hover_filter)
      end
      dock.setProperty("pocket_hover_ready", true)
    end
    headers.each do |header|
      header.setBackgroundRole(RBA::QPalette::Button)
      header.setForegroundRole(RBA::QPalette::ButtonText)
    end
    dock.setProperty("pocket_tools_theme", name)
  end

  def self.refresh_layer_toolbox_selection(dock)
    view = RBA::MainWindow.instance.current_view
    selected = view && view.selected_layers.to_a
    layer = view && (selected.first || view.current_layer).current
    # KLayout names the frame palette "colors" and the fill palette "colors_frame".
    colors = layer ? { "colors" => layer.frame_color, "colors_frame" => layer.fill_color } : {}
    walk = lambda do |parent|
      parent.children.each do |child|
        if child.objectName == "button" && child.respond_to?(:text) && child.text.to_s.empty? &&
           child.parent && child.parent.objectName == "color_f"
          section = child.parent.parent
          target = section && colors[section.objectName]
          is_selected = target && (child.palette.color(RBA::QPalette::Window).rgb.to_i & 0xffffff) == (target.to_i & 0xffffff)
          is_selected = !!is_selected
          if child.property("pocket_selected") != is_selected
            child.setProperty("pocket_selected", is_selected)
            child.setLineWidth(is_selected || child.property("pocket_hovered") ? 2 : 1)
          end
        end
        walk.call(child)
      end
    end
    walk.call(dock)
  rescue StandardError => e
    # A closed view can invalidate its Qt wrappers while the toolbox remains.
    message = "#{e.class}: #{e.message}"
    if @toolbox_selection_error != message
      @toolbox_selection_error = message
      warn "Pocket Themes Layer Toolbox selection: #{message}"
    end
  end

  # The native DRC Info panel embeds blue in its rich text instead of using
  # the palette. Change only that foreground, preserving links, images and text.
  def self.refresh_drc_info
    @drc_panels ||= {}
    RBA::QApplication.allWidgets.each do |widget|
      if widget.objectName == "lt_dock_widget"
        style_layer_toolbox(widget, @applied_theme || saved_theme)
        refresh_layer_toolbox_selection(widget)
        next
      end
      next unless widget.objectName == "info_text"
      parent = widget.parent
      parent = parent.parent while parent && !%w[MarkerBrowserPage MarkerBrowserDialog].include?(parent.objectName)
      next unless parent
      key = widget.object_id
      unless @drc_panels.key?(key)
        state = { widget: widget, dirty: true, updating: false }
        @drc_panels[key] = state
        widget.document.contentsChanged do
          unless state[:updating]
            state[:dirty] = true
            recolor_drc_info(state)
          end
        end
        widget.destroyed { @drc_panels.delete(key) }
      end
    end
    @drc_panels.each_value do |state|
      recolor_drc_info(state) if state[:dirty]
    end
  rescue StandardError => e
    drc_contrast_error(e)
  end

  # Apply within contentsChanged, before the new result can be painted. The
  # timer only discovers panels created after startup, not text updates.
  def self.recolor_drc_info(state)
    return if state[:updating]
    state[:dirty] = false
    state[:updating] = true
    begin
      document = state[:widget].document
      target = RBA::QColor.new(@drc_text_color)
      known = ["#0000ff"] + THEMES.values.map { |theme| theme[:link] }
      ranges = []
      block = document.begin
      while block.isValid
        iterator = block.begin
        until iterator.atEnd
          fragment = iterator.fragment
          if fragment.isValid
            format = fragment.charFormat
            color = format.foreground.color.name
            if !format.isAnchor && format.fontWeight >= RBA::QFont::Bold && known.include?(color) && color != target.name
              ranges << [fragment.position, fragment.length]
            end
          end
          iterator.send(:'++')
        end
        block = block.next
      end
      ranges.each do |position, length|
        cursor = RBA::QTextCursor.new(document)
        cursor.setPosition(position)
        cursor.setPosition(position + length, RBA::QTextCursor::KeepAnchor)
        format = RBA::QTextCharFormat.new
        format.setForeground(RBA::QBrush.new(target))
        cursor.mergeCharFormat(format)
      end
    ensure
      state[:updating] = false
    end
  rescue StandardError => e
    drc_contrast_error(e)
  end

  def self.drc_contrast_error(e)
    @drc_error = "#{e.class}: #{e.message}"
    @drc_timer.stop if @drc_timer
    warn "Pocket Themes DRC contrast: #{@drc_error}"
  end

  def self.preference_path
    File.join(RBA::Application.instance.application_data_path, "pocket-theme.txt")
  end

  def self.saved_theme
    File.read(preference_path).strip
  rescue Errno::ENOENT
    "dark"
  rescue SystemCallError => e
    warn "Pocket Themes: #{e.message}"
    "dark"
  end

  def self.apply(name, save: false)
    name = "dark" unless THEMES.key?(name)
    colors = THEMES.fetch(name)
    # Avoid repolishing every Qt widget when Macro Development reloads this
    # autorun macro without a theme change.
    if @applied_theme != name
      palette = RBA::QPalette.new
      roles = {
        Window: :window, WindowText: :text, Base: :base, AlternateBase: :alternate,
        Text: :text, Button: :button, ButtonText: :text, Highlight: :accent,
        ToolTipBase: :button, ToolTipText: :text, Link: :link, LinkVisited: :link
      }
      roles.each do |role, key|
        palette.setColor(RBA::QPalette.const_get(role), RBA::QColor.new(colors.fetch(key)))
      end
      palette.setColor(RBA::QPalette::HighlightedText, RBA::QColor.new("#ffffff"))
      [:WindowText, :Text, :ButtonText].each do |role|
        palette.setColor(
          RBA::QPalette::Disabled, RBA::QPalette.const_get(role),
          RBA::QColor.new(colors.fetch(:muted))
        )
      end
      RBA::QApplication.setPalette(palette)

      # Model foregrounds carry meaning (e.g. green DRC categories with no errors).
      # Keep those colors native; force text only inside the Salt package dialog.
      stylesheet = @original_css + <<~CSS
        QAbstractItemView {
          background-color: #{colors[:base]};
          alternate-background-color: #{colors[:alternate]};
          selection-background-color: #{colors[:accent]};
          selection-color: #ffffff;
        }
        QDialog#SaltManagerDialog QAbstractItemView,
        QDialog#SaltManagerDialog QAbstractItemView::item { color: #{colors[:text]}; }
        QAbstractItemView::item:selected,
        QDialog#SaltManagerDialog QAbstractItemView::item:selected {
          color: #ffffff;
          background-color: #{colors[:accent]};
        }
        QHeaderView::section {
          color: #{colors[:text]};
          background-color: #{colors[:button]};
        }
      CSS
      RBA::QApplication.instance.setStyleSheet(stylesheet)
      RBA::Application.instance.set_config("background-color", colors.fetch(:base))
      @applied_theme = name
    end
    @drc_text_color = %w[dark graphite solarized_dark].include?(name) ? colors[:link] : "#0000ff"
    (@drc_panels || {}).each_value { |state| state[:dirty] = true }
    refresh_drc_info
    @actions.each { |key, action| action.checked = key == name }
    File.write(preference_path, name + "\n") if save
  rescue SystemCallError => e
    warn "Pocket Themes: #{e.message}"
  end

  def self.install
    menu = RBA::MainWindow.instance.menu
    @original_css ||= RBA::QApplication.instance.styleSheet.to_s
    RBA::QApplication.setStyle("Fusion") unless @fusion_applied
    @fusion_applied = true
    menu.delete_item(MENU) if menu.is_valid?(MENU)
    menu.delete_item("view_menu.pocket_themes") if menu.is_valid?("view_menu.pocket_themes")
    menu.insert_menu("view_menu.end", "pocket_tools", "Pocket Tools") unless menu.is_valid?(ROOT)
    menu.insert_menu("#{ROOT}.end", "themes", "Themes")
    @actions = {}
    THEMES.each do |key, colors|
      action = RBA::Action.new
      action.title = colors.fetch(:title)
      action.checkable = true
      action.on_triggered { apply(key, save: true) }
      menu.insert_item("#{MENU}.end", key, action)
      @actions[key] = action
    end
    apply(saved_theme)
    @drc_timer.stop if @drc_timer
    @drc_timer = RBA::QTimer.new(RBA::MainWindow.instance)
    @drc_timer.timeout { refresh_drc_info }
    @drc_timer.start(250)
  end
end

PocketThemes.install
