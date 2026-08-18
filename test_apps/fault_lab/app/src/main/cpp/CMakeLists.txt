cmake_minimum_required(VERSION 3.22.1)
project(faultlab LANGUAGES CXX)

add_library(faultlab SHARED faultlab.cpp)
target_link_libraries(faultlab log)
