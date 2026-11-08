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
    palette = RBA::QPalette.new
    roles = {
      Window: :window, WindowText: :text, Base: :base, AlternateBase: :alternate,
      Text: :text, Button: :button, ButtonText: :text, Highlight: :accent,
      ToolTipBase: :button, ToolTipText: :text, Link: :link
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

    # Keep item colors legible in dialogs such as the Salt package manager.
    RBA::QApplication.instance.setStyleSheet(@original_css + <<~CSS)
      QAbstractItemView {
        color: #{colors[:text]};
        background-color: #{colors[:base]};
        alternate-background-color: #{colors[:alternate]};
        selection-background-color: #{colors[:accent]};
        selection-color: #ffffff;
      }
      QAbstractItemView::item { color: #{colors[:text]}; }
      QAbstractItemView::item:selected {
        color: #ffffff;
        background-color: #{colors[:accent]};
      }
      QHeaderView::section {
        color: #{colors[:text]};
        background-color: #{colors[:button]};
      }
    CSS

    RBA::Application.instance.set_config("background-color", colors.fetch(:base))
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
  end
end

PocketThemes.install
