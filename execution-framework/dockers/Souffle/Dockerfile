################################################################################
#souffle
################################################################################
FROM ubuntu:22.04

# Install latest updates and dependencies
RUN apt-get update
RUN apt-get upgrade -y
RUN apt-get install bash

RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev python3.9 python3-pip python3-setuptools python3-dev
RUN apt-get install git -y
RUN apt-get install bison -y 
RUN apt-get install build-essential -y
RUN apt-get install clang -y  
RUN apt-get install  cmake -y 
 RUN apt-get install doxygen -y
RUN apt-get install  flex -y
RUN apt-get install  g++ -y
RUN apt-get install  git -y
RUN apt-get install  libffi-dev -y
RUN apt-get install  libncurses5-dev -y
RUN apt-get install  libsqlite3-dev -y
RUN apt-get install  make -y
RUN apt-get install  mcpp -y
 RUN apt-get install sqlite -y
 RUN apt-get install zlib1g-dev -y
RUN apt-get install -y openjdk-17-jdk less vim wget unzip	
# Download Souffle release
RUN mkdir souffle 
RUN git clone https://github.com/souffle-lang/souffle.git /souffle

WORKDIR /souffle

RUN cmake -S . -B build -DCMAKE_INSTALL_PREFIX=.
RUN cmake --build build -j8 --target install

COPY rulegen.jar /souffle/rulegen.jar
COPY reverseR2RML.py /souffle/reverseR2RML.py
COPY functors.cpp /souffle/functors.cpp

# Build custom Souffle functors as a shared library.
RUN mkdir -p /souffle/lib && \
	g++ -fPIC -shared -std=c++17 \
	-I/souffle/include \
	/souffle/functors.cpp \
	-o /souffle/lib/libfunctors.so

# Expose folders
RUN mkdir /data
RUN mkdir /mappings
RUN mkdir /results
RUN mkdir /configs
ENV PATH="/souffle/bin:${PATH}"
ENV LD_LIBRARY_PATH="/souffle/lib:${LD_LIBRARY_PATH}"

# Silent
CMD ["tail", "-f", "/dev/null"]
