require "fileutils"

iconset_path, output_path = ARGV
abort "Usage: ruby make-logistics-tower-icns.rb <iconset> <output.icns>" unless iconset_path && output_path

sizes = [
  ["icp4", "icon_16x16.png"], ["icp5", "icon_32x32.png"],
  ["icp6", "icon_64x64.png"], ["ic07", "icon_128x128.png"],
  ["ic08", "icon_256x256.png"], ["ic09", "icon_512x512.png"],
  ["ic10", "icon_512x512@2x.png"]
]
chunks = sizes.map do |type, filename|
  png = File.binread(File.join(iconset_path, filename))
  [type, 8 + png.bytesize].pack("a4N") + png
end
total_length = 8 + chunks.sum(&:bytesize)
File.binwrite(output_path, ["icns", total_length].pack("a4N") + chunks.join)
