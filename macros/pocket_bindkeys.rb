mw = RBA::MainWindow.instance

bindings = {
  "edit_menu.mode_menu.move"    => "M",
  "edit_menu.mode_menu.select"  => "S",
  "edit_menu.mode_menu.ruler"   => "K",
  "edit_menu.clear_all_rulers"  => "Ctrl+D",
  "edit_menu.mode_menu.polygon" => "P",
  "edit_menu.mode_menu.box"     => "B",
  "edit_menu.mode_menu.text"    => "T",
  "edit_menu.mode_menu.path"    => "Ctrl+S",
  "zoom_menu.zoom_fit"         => "F"
}

# Liberar las combinaciones solicitadas de otras acciones.
changes = {}

mw.get_key_bindings.each do |action, shortcut|
  if bindings.values.include?(shortcut) && !bindings.key?(action)
    changes[action] = ""
  end
end

# Aplicar los nuevos atajos.
mw.set_key_bindings(changes.merge(bindings))